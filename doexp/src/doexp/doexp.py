#!/usr/bin/env python3
# See https://github.com/krzentner/doexp
from dataclasses import dataclass, field, replace
from typing import Union, List, Tuple, Optional, Set, Any, Dict
import os
import re
import subprocess
import time
import shutil
import math
import sys
import argparse
import shlex
import tempfile
import csv
import io

import psutil


@dataclass(frozen=True)
class FileArg:
    filename: str


@dataclass(frozen=True)
class In(FileArg):
    pass


@dataclass(frozen=True)
class Out(FileArg):
    pass


@dataclass(frozen=True)
class Cmd:
    args: Tuple[Union[str, FileArg], ...]
    extra_outputs: Tuple[Out, ...]
    extra_inputs: Tuple[In, ...]
    warmup_time: float = 1.0
    ram_gb: float = 4.0
    priority: Union[int, Tuple[int, ...]] = 10
    gpus: Union[str, None] = None
    gpu_ram_gb: float = 0.0
    cores: Optional[int] = None
    skypilot_template: Optional[str] = None
    env: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self):
        assert (
            self.gpus is None or self.gpu_ram_gb == 0.0
        ), "Only gpus or gpu_ram_gb should be passed"
        assert all([isinstance(input, In) for input in self.extra_inputs])
        assert all([isinstance(output, Out) for output in self.extra_outputs])

    def __str__(self):
        args = _cmd_to_args(self, "data", "tmp_data")
        return " ".join(args)

    def to_shell(self, data_dir, tmp_data_dir):
        args = _cmd_to_args(self, data_dir, tmp_data_dir)
        return " ".join(shlex.quote(arg) for arg in args)

    def _cores_as_int(self):
        if self.cores is None:
            return 1
        else:
            return self.cores


@dataclass
class Process:
    cmd: Cmd
    proc: subprocess.Popen

    # Needed to update gpu_ram_reserved
    cuda_devices: List[int]

    # Will be increased if process exceeds amount specified in cmd
    max_ram_gb: float


_BYTES_PER_GB = (1024) ** 3


def printv(verbose, *args, **kwargs):
    if verbose:
        print(*args, **kwargs)


def get_cuda_gpus() -> Tuple[str, ...]:
    visible_devices = os.environ.get("CUDA_VISIBLE_DEVICES", None)
    if visible_devices is None:
        try:
            smi_proc = subprocess.run(
                ["nvidia-smi", "--list-gpus"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            output = smi_proc.stdout.decode("utf-8")
            return tuple(re.findall(r"GPU ([0-9]*):", output))
        except FileNotFoundError:
            return ()
    else:
        if visible_devices == "-1":
            return ()
        try:
            return tuple([str(int(d)) for d in visible_devices.split(",")])
        except ValueError:
            return ()


def get_cuda_vram(devices: List[str]):
    smi_proc = subprocess.run(
        ["nvidia-smi", "--query-gpu=gpu_name,index,memory.free", "--format=csv"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    mem_free_gb = [0. for _ in devices]
    output = smi_proc.stdout.decode("utf-8")
    for row in csv.DictReader(io.StringIO(output), delimiter=","):
        row = {k.strip(): v.strip() for (k, v) in row.items()}
        if row["index"] in devices:
            i = devices.index(row["index"])
            free_mib = row["memory.free [MiB]"]
            print(f"{row['name']}: {free_mib} free")
            free_gb = int(free_mib.split(" ")[0]) / 1024
            mem_free_gb[i] = free_gb
    for i, val in enumerate(mem_free_gb):
        if val == 0.:
            print(f"Could not get free memory for GPU {devices[i]}")
    return mem_free_gb


def _ram_in_use_gb():
    vm = psutil.virtual_memory()
    return (vm.total - vm.available) / _BYTES_PER_GB


def _filter_cmds_remaining(commands, data_dir):
    out_commands = set()
    for cmd in commands:
        found_out = False
        for arg in cmd.args + cmd.extra_outputs:
            if isinstance(arg, Out):
                found_out = True
            if isinstance(arg, Out) and not os.path.exists(
                os.path.join(data_dir, arg.filename)
            ):
                out_commands.add(cmd)
        if not found_out:
            print("No outputs for command:", str(cmd))
    return out_commands


def _filter_cmds_ready(commands, data_dir, verbose):
    out_commands = set()
    for cmd in commands:
        ready = True
        for arg in cmd.args + cmd.extra_inputs:
            if isinstance(arg, In) and not os.path.exists(
                os.path.join(data_dir, arg.filename)
            ):
                ready = False
                printv(verbose, "Waiting on input:", arg.filename)
                break
        if ready:
            out_commands.add(cmd)
    return out_commands


def _filter_cmds_ram(commands, *, reserved_ram_gb, ram_gb_cap, use_skypilot, verbose):
    out_commands = set()
    for cmd in commands:
        if use_skypilot and cmd.skypilot_template:
            out_commands.add(cmd)
        elif max(reserved_ram_gb, _ram_in_use_gb()) + cmd.ram_gb <= ram_gb_cap:
            out_commands.add(cmd)
        else:
            printv(verbose, "Not enough ram free to run:", cmd)
    return out_commands


def _filter_cmds_cores(
    commands, *, reserved_cores, max_core_alloc, use_skypilot, verbose
):
    out_commands = set()
    for cmd in commands:
        cores = cmd._cores_as_int()
        if use_skypilot and cmd.skypilot_template:
            out_commands.add(cmd)
        elif reserved_cores + cores <= max_core_alloc:
            out_commands.add(cmd)
        else:
            printv(verbose, "Not enough cores free to run:", cmd)
    return out_commands


def _filter_cmds_gpu_ram(
    commands, *, gpu_ram_reserved, gpu_ram_cap, use_skypilot, verbose
):
    out_commands = set()
    gpu_ram_free = [
        cap - reserved
        for (cap, reserved) in zip(gpu_ram_cap, gpu_ram_reserved)
    ]
    for cmd in commands:
        if use_skypilot and cmd.skypilot_template:
            out_commands.add(cmd)
        elif cmd.gpus and all(resv == 0.0 for resv in gpu_ram_reserved):
            out_commands.add(cmd)
        elif cmd.gpus is None and min(gpu_ram_free, default=0.) >= cmd.gpu_ram_gb:
            out_commands.add(cmd)
        else:
            printv(verbose, "Not enough gpu ram free to run:", cmd)
    return out_commands


def _sort_cmds(commands):
    def key(cmd):
        priority_as_list = cmd.priority
        if not isinstance(priority_as_list, (list, tuple)):
            priority_as_list = [priority_as_list]

        return ([-prio for prio in priority_as_list], cmd.warmup_time, cmd.ram_gb)

    return sorted(list(commands), key=key)

def create_paths(cmd, data_dir, tmp_data_dir):
    print('Creating paths')
    for arg in cmd.args:
        if isinstance(arg, In):
            d = os.path.join(data_dir, arg.filename)
            if not os.path.exists(d):
                print(f'Missing In file {d}')
        elif isinstance(arg, (Out, FileArg)):
            # Use temporary directory here
            d = os.path.join(tmp_data_dir, arg.filename)
            extra = None
            while not extra:
                d, extra = os.path.split(d)
            print(f"Creating directory ({d}) for Out file ({arg.filename}).")
            os.makedirs(d, exist_ok=True)


def _cmd_to_args(cmd, data_dir, tmp_data_dir):
    args = []
    for arg in cmd.args:
        if isinstance(arg, In):
            arg = os.path.join(data_dir, arg.filename)
        elif isinstance(arg, (Out, FileArg)):
            # Use temporary directory here
            arg = os.path.join(tmp_data_dir, arg.filename)
        args.append(str(arg))
    return args


def _filter_completed(running):
    for p in running:
        p.proc.poll()
    completed = [p for p in running if p.proc.returncode is not None]
    now_running = [p for p in running if p not in completed]
    return now_running, completed


def _cmd_name(cmd):
    args = []
    for arg in cmd.args:
        if isinstance(arg, (In, Out)):
            args.append(arg.filename)
        else:
            args.append(str(arg))
    name = " ".join(args).replace("/", "\u2571")
    if len(name) > 200:
        name = name[:200]
    return name


@dataclass
class Context:
    commands: Set[Cmd] = field(default_factory=set)
    running: List[Process] = field(default_factory=list)
    data_dir: str = f"{os.getcwd()}/data"
    temporary_data_dir: Optional[str] = None
    verbose: bool = False
    verbose_now: bool = False
    _vm_percent_cap: float = 90.0

    # Affects slurm and skypilot as well
    max_concurrent_jobs: int or None = psutil.cpu_count()

    # Affects slurm and skypilot as well
    max_core_alloc: int or None = psutil.cpu_count()

    # These fields record allocations, etc.
    reserved_cores: int = 0
    warmup_deadline: float = time.monotonic()
    reserved_ram_gb: float = _ram_in_use_gb()
    last_commands_remaining: int = -1
    next_cuda_device: int = 0
    _cuda_devices: List[str] = get_cuda_gpus()
    gpu_ram_cap: List[float] = field(default_factory=list)
    gpu_ram_reserved: List[float] = field(default_factory=list)

    # External runner fields
    srun_availabe: bool = bool(shutil.which("srun"))
    use_slurm: Optional[bool] = None
    use_skypilot: bool = False

    @property
    def _tmp_data_dir(self):
        if self.temporary_data_dir is not None:
            return self.temporary_data_dir
        else:
            return f"{self.data_dir}_tmp"

    @property
    def ram_gb_cap(self):
        return (
            self._vm_percent_cap
            * psutil.virtual_memory().total
            / (100.0 * _BYTES_PER_GB)
        )

    @property
    def vm_percent_cap(self):
        return self._vm_percent_cap

    @vm_percent_cap.setter
    def vm_percent_cap(self, value):
        self._vm_percent_cap = value

    def cmd(self, command):
        self.commands.add(command)

    def run_all(self, args):
        """Runs all commands listed in the file."""
        self.verbose = args.verbose
        self.data_dir = args.data_dir
        self.temporary_data_dir = args.tmp_dir
        self.use_slurm = args.use_slurm
        self.use_skypilot = args.use_skypilot
        if self.use_skypilot:
            print("WARNING: Using skypilot. Watch usage to avoid excessive bills.")
        if self.srun_availabe and self.use_slurm is None:
            print("srun is available. Either pass --use-slurm or --no-use-slurm")
            return
        elif self.use_slurm and not self.srun_availabe:
            print("srun is not available, cannot use slurm")
            return
        elif not self.use_slurm:
            print(f"Using GPUS: {self._cuda_devices}")
            self.gpu_ram_cap = get_cuda_vram(self._cuda_devices)
            self.gpu_ram_reserved = [0.0 for _ in self.gpu_ram_cap]
            self._choose_next_cuda_device()
        done = False
        while not done:
            ready_cmds, done = self._refresh_commands(args.expfile, args.dry_run)
            if self._ready() and ready_cmds:
                self.run_cmd(ready_cmds[0])
                done = False
            if not self.use_slurm:
                self._terminate_if_oom()
            self.running, completed = _filter_completed(self.running)
            if self.running:
                done = False
            if not completed:
                # Don't hard-loop
                time.sleep(0.2)
            self._process_completed(completed)

    def _choose_next_cuda_device(self):
        if self.gpu_ram_cap:
            # Find gpu with most ram free, cycling in case all utilization is
            # equal
            gpu_ram_free = [
                cap - reserved
                for (cap, reserved) in zip(self.gpu_ram_cap, self.gpu_ram_reserved)
            ]
            max_free = max(gpu_ram_free)
            while True:
                self.next_cuda_device = (self.next_cuda_device + 1) % len(
                    self._cuda_devices
                )
                if gpu_ram_free[self.next_cuda_device] >= max_free:
                    break

    def _ready(self):
        """Checks global readiness conditions."""
        if (
            self.max_concurrent_jobs is not None
            and len(self.running) >= self.max_concurrent_jobs
        ):
            return False
        if time.monotonic() < self.warmup_deadline:
            return False
        return True

    def _refresh_commands(self, filename, dry_run):
        """Reloads, filters, and sorts the commands"""
        old_commands = self.commands
        self.commands = set()
        content = ""
        try:
            with open(filename) as f:
                content = f.read()
                exec(content, {})
        except Exception as exc:
            if isinstance(exc, KeyboardInterrupt):
                raise exc
            else:
                try:
                    line_num = sys.exc_info()[2].tb_next.tb_lineno
                    print(f"Error in exps.py (line {line_num}):")
                    print(exc)
                    print(">>", content.split("\n")[line_num - 1])
                except (AttributeError, IndexError):
                    print(exc)
                self.commands = old_commands
        if dry_run:
            for cmd in _sort_cmds(self.commands):
                print(cmd.to_shell(self.data_dir, self._tmp_data_dir))
            return set(), True
        ready, done, remaining = self._filter_commands(self.commands)
        if len(remaining) != self.last_commands_remaining:
            self.last_commands_remaining = len(remaining)
            print("Number of commands:", len(self.commands))
            print("Commands remaining:", len(remaining))
            self.verbose_now = self.verbose
        else:
            self.verbose_now = False
        return _sort_cmds(ready), done

    def _filter_commands(self, commands):
        """Filters the commands to find only those that are ready to run"""
        needs_output = _filter_cmds_remaining(commands, self.data_dir)
        has_inputs = _filter_cmds_ready(needs_output, self.data_dir, self.verbose_now)
        if needs_output and not has_inputs and not self.running:
            print("Commands exist without any way to acquire inputs:")
            for cmd in needs_output:
                print(str(cmd))
        if self.use_slurm:
            fits_in_gpu = has_inputs
        else:
            fits_in_ram = _filter_cmds_ram(
                has_inputs,
                reserved_ram_gb=self.reserved_ram_gb,
                ram_gb_cap=self.ram_gb_cap,
                use_skypilot=self.use_skypilot,
                verbose=self.verbose_now,
            )
            fits_in_gpu = _filter_cmds_gpu_ram(
                fits_in_ram,
                gpu_ram_reserved=self.gpu_ram_reserved,
                gpu_ram_cap=self.gpu_ram_cap,
                use_skypilot=self.use_skypilot,
                verbose=self.verbose_now,
            )

        fits_in_core_alloc = _filter_cmds_cores(
            fits_in_gpu,
            reserved_cores=self.reserved_cores,
            max_core_alloc=self.max_core_alloc,
            use_skypilot=self.use_skypilot,
            verbose=self.verbose_now,
        )
        not_running = self._filter_cmds_running(fits_in_core_alloc)
        return not_running, not bool(needs_output), needs_output

    def _filter_cmds_running(self, commands):
        """Filters out running commands"""
        out_commands = set()
        for cmd in commands:
            running = False
            for process in self.running:
                if cmd == process.cmd:
                    running = True
                    break
            if not running:
                out_commands.add(cmd)
        return out_commands

    def run_cmd(self, cmd):
        """Sets temp files and starts a process for cmd"""
        self.reserved_ram_gb += cmd.ram_gb
        self.reserved_cores += cmd._cores_as_int()
        cmd_dir = os.path.join(self._tmp_data_dir, "pipes", _cmd_name(cmd))
        os.makedirs(cmd_dir, exist_ok=True)
        stdout = open(os.path.join(cmd_dir, "stdout.txt"), "w")
        stderr = open(os.path.join(cmd_dir, "stderr.txt"), "w")
        # print(cmd.to_shell(self.data_dir))
        process = self._run_process(
            cmd,
            stdout=stdout,
            stderr=stderr,
        )
        print(process.proc.pid)
        self.warmup_deadline = time.monotonic() + cmd.warmup_time
        self.running.append(process)
        return process

    def _run_process(self, cmd, *, stdout, stderr):
        args = _cmd_to_args(cmd, self.data_dir, self._tmp_data_dir)
        create_paths(cmd, self.data_dir, self._tmp_data_dir)
        env = os.environ.copy()
        cuda_devices = []
        for k, v in cmd.env:
            env[k] = v
        if self.use_skypilot and cmd.skypilot_template:
            args = self._skypilot_args(cmd)
        elif self.use_slurm:
            args = self._slurm_args(cmd, args)
        elif cmd.gpus is not None:
            env["CUDA_VISIBLE_DEVICES"] = cmd.gpus
            # The meaning of indices in cmd.gpus could be different from doexp
            # internal indices, so reserve all gpus.
            for i, cap in enumerate(self.gpu_ram_cap):
                self.gpu_ram_reserved[i] = cap
            cuda_devices = list(range(len(self.gpu_ram_cap)))
        elif self._cuda_devices:
            env["CUDA_VISIBLE_DEVICES"] = str(self._cuda_devices[self.next_cuda_device])
            self.gpu_ram_reserved[self.next_cuda_device] += cmd.gpu_ram_gb
            cuda_devices = [self.next_cuda_device]

            self._choose_next_cuda_device()

        print(" ".join(shlex.quote(arg) for arg in args))
        proc = subprocess.Popen(args, stdout=stdout, stderr=stderr, env=env)
        return Process(cmd=cmd, proc=proc, cuda_devices=cuda_devices, max_ram_gb=cmd.ram_gb)

    def _skypilot_args(self, cmd):
        tmp_dir_rel_path = os.path.relpath(self._tmp_data_dir, os.getcwd())
        data_dir_rel_path = os.path.relpath(self.data_dir, os.getcwd())
        args_rel = _cmd_to_args(cmd, data_dir_rel_path, tmp_dir_rel_path)
        command = " ".join([shlex.quote(arg) for arg in args_rel])
        with open(cmd.skypilot_template) as f:
            template_content = f.read()
        skypilot_yaml = template_content.format(command=command)
        skypilot_file = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        skypilot_file.write(skypilot_yaml)
        skypilot_file.close()
        args = [
            "python",
            "-m",
            "doexp.skypilot_wrapper",
            "--task-file",
            skypilot_file.name,
        ]
        for arg in cmd.args:
            if isinstance(arg, (Out, FileArg)) and not isinstance(arg, In):
                args.append("--out-file")
                f_path = os.path.join(tmp_dir_rel_path, arg.filename)
                args.append(f_path)
        return args

    def _slurm_args(self, cmd, args):
        ram_mb = int(math.ceil(1024 * cmd.ram_gb))
        if not cmd.cores:
            core_args = ()
            mb_per_core = int(1024 * cmd.ram_gb)
        else:
            core_args = (f"--cpus-per-task={cmd.cores}",)
            mb_per_core = int(math.ceil(1024 * cmd.ram_gb / cmd.cores))
        args = [
            "srun",
            *core_args,
            f"--mem-per-cpu={mb_per_core}M",
            "--",
        ] + args
        return args

    def _terminate_if_oom(self):
        """Terminates processes if over ram cap"""
        gb_free = self.ram_gb_cap - _ram_in_use_gb()

        def total_time(process):
            try:
                times = psutil.Process(process.proc.pid).cpu_times()
                return (
                    times.user + times.system + times.children_user + times.children_system
                )
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                return float('inf')

        by_total_time = sorted(self.running, key=total_time)
        for process in by_total_time:
            try:
                mem = psutil.Process(process.proc.pid).memory_full_info()
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            ram_gb = (getattr(mem, 'pss', 0) + mem.uss) / _BYTES_PER_GB
            if ram_gb > process.cmd.ram_gb:
                print(
                    f"Command exceeded memory limit "
                    f"({ram_gb} > {process.cmd.ram_gb}): "
                    f"{_cmd_name(process.cmd)}"
                )
                self.reserved_ram_gb -= process.max_ram_gb
                process.max_ram_gb = ram_gb
                self.reserved_ram_gb += process.max_ram_gb
            if gb_free < 0:
                print(f"Terminating process: {_cmd_name(process.cmd)}")
                process.proc.terminate()
                gb_free += ram_gb

    def _process_completed(self, completed):
        """Copy outputs from the tmp dir if the process exited successfully"""
        for process in completed:
            cmd = process.cmd
            self.reserved_ram_gb -= cmd.ram_gb
            self.reserved_cores -= cmd._cores_as_int()
            for cuda_dev in process.cuda_devices:
                if cmd.gpus:
                    self.gpu_ram_reserved[cuda_dev] = 0
                else:
                    self.gpu_ram_reserved[cuda_dev] -= cmd.gpu_ram_gb
            if process.proc.returncode != 0:
                print(f"Error running {str(cmd)}")
                cmd_dir = os.path.join(self._tmp_data_dir, "pipes", _cmd_name(cmd))
                with open(os.path.join(cmd_dir, "stderr.txt")) as f:
                    print(f.read())
            else:
                print(f"Command complete: {str(cmd)}")
                for arg in cmd.args + cmd.extra_outputs:
                    if isinstance(arg, Out):
                        tmp = os.path.join(self._tmp_data_dir, arg.filename)
                        final = os.path.join(self.data_dir, arg.filename)
                        os.makedirs(os.path.split(final)[0], exist_ok=True)
                        try:
                            if os.path.isdir(tmp):
                                shutil.copytree(tmp, final, dirs_exist_ok=True)
                            else:
                                shutil.copy2(tmp, final)
                        except Exception as exc:
                            if isinstance(exc, KeyboardInterrupt):
                                raise exc
                            print(f"Could not copy output {tmp} for command {cmd}")


GLOBAL_CONTEXT = Context()


def cmd(
    *args,
    priority: Union[int, List[int], Tuple[int, ...]] = 10,
    ram_gb: float = 4,
    cores: Optional[int] = None,
    warmup_time: float = 1.0,
    extra_outputs: Union[List[Union[str, Out]], Tuple[Union[str, Out], ...]] = tuple(),
    extra_inputs: Union[List[Union[str, In]], Tuple[Union[str, In], ...]] = tuple(),
    gpus: Union[str, None] = None,
    gpu_ram_gb: float = 0.0,
    skypilot_template: Optional[str] = None,
    env: Dict[str, Any] = None,
) -> None:
    """Add a command to be run by the GLOBAL_CONTEXT.

    Args:
        - `priority`: an int (or tuple of ints) that defines the priority of the command (higher priority runs first). Typically used to ensure an even spread across experiments by using `-seed` as a priority.
        - `ram_gb`: a float (or int) of the expected GiB of RAM the command will use. This is softly enforced when run locally, and strongly enforced when using `slurm`. Note that a maximum RAM usage percentile (90% by default) is strictly enforced even when running locally to avoid thrashing.
        - `cores`: a number of cores that the command needs. Strongly enforced when using `slurm`.
        - `warmup_time`: a number of seconds to wait after running a command before starting another command. Useful to avoid hitting rate limits or overloading systems by starting too many processes at once.
        - `extra_outputs`: a tuple of `Out` files that will be created by the command, but which are not present in the arguments.
        - `extra_inputs`: a tuple of `In` files required by the command, but which are not present in the arguments. Often used to emulate globbing.
        - `gpus`: an optional string declaring which gpus the command should have access to. If not passed, `CUDA_VISIBLE_DEVICES` will be used to assign GPUs in a round-robin fashion.
        - `gpu_ram_gb`: A number of GiB of GPU VRAM required. Must not be passed with `gpus` (which override this option).
        - `skypilot_template`: a path to a skypilot yaml file that contains a replacement sequence `{command}` in it. A command must specify a `skypilot_template` to use skypilot, and one skypilot cluster will be created using the template per command. See `examples/skypilot_template.yaml` for an example.
        - `env`: Overrides to the environment variables.

    """
    if isinstance(priority, list):
        priority = tuple(priority)

    extra_outputs = tuple(
        [output if isinstance(output, Out) else Out(output) for output in extra_outputs]
    )
    extra_inputs = tuple(
        [input if isinstance(input, In) else In(input) for input in extra_inputs]
    )
    if env is None:
        env = ()
    else:
        env = tuple([(str(k), str(v)) for (k, v) in env.items()])
    GLOBAL_CONTEXT.cmd(
        Cmd(
            args=tuple(args),
            extra_outputs=extra_outputs,
            extra_inputs=extra_inputs,
            warmup_time=warmup_time,
            ram_gb=ram_gb,
            priority=priority,
            gpus=gpus,
            gpu_ram_gb=gpu_ram_gb,
            cores=cores,
            skypilot_template=skypilot_template,
            env=env,
        )
    )


def parse_args():
    parser = argparse.ArgumentParser("doexp.py")
    parser.add_argument("expfile", nargs="?", default="exps.py")
    # data_dir: str = os.path.expanduser("~/exp_data")
    parser.add_argument("-d", "--data-dir", default=f"{os.getcwd()}/data")
    parser.add_argument("-t", "--tmp-dir", default=f"{os.getcwd()}/data_tmp")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--use-skypilot", action="store_true")
    parser.add_argument("--use-slurm", action="store_true")
    parser.add_argument("--no-use-slurm", dest="use_slurm", action="store_false")
    parser.set_defaults(use_slurm=None)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main():
    if __name__ == "__main__":
        sys.modules["doexp"] = sys.modules["__main__"]
    import doexp

    assert doexp.GLOBAL_CONTEXT is GLOBAL_CONTEXT
    doexp.GLOBAL_CONTEXT.run_all(parse_args())


if __name__ == "__main__":
    main()
