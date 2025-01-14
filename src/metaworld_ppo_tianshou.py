#!/usr/bin/env python3
import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import argparse
import datetime
import os
import pprint
import copy

import numpy as np
import torch
from torch import nn
from torch.distributions import Independent, Normal
from torch.optim.lr_scheduler import LambdaLR
from torch.utils.tensorboard import SummaryWriter

from tianshou.data import Collector, ReplayBuffer, VectorReplayBuffer
from tianshou.policy import PPOPolicy
from tianshou.trainer import onpolicy_trainer
from tianshou.utils import TensorboardLogger, WandbLogger
from tianshou.utils.net.common import Net
from tianshou.utils.net.continuous import ActorProb, Critic

from metaworld_env_tianshou import make_metaworld_env

def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=str, default="window-open")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--buffer-size", type=int, default=50_000)
    parser.add_argument("--hidden-sizes", type=int, nargs="*", default=[128, 128])
    parser.add_argument("--lr", type=float, default=3e-4)
    parser.add_argument("--gamma", type=float, default=0.99)
    parser.add_argument("--epoch", type=int, default=400)
    parser.add_argument("--step-per-epoch", type=int, default=50_000)
    parser.add_argument("--step-per-collect", type=int, default=50_000)
    parser.add_argument("--repeat-per-collect", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--training-num", type=int, default=1)
    parser.add_argument("--test-num", type=int, default=1)
    # ppo special
    parser.add_argument("--rew-norm", type=int, default=True)
    parser.add_argument("--vf-coef", type=float, default=0.25)
    parser.add_argument("--ent-coef", type=float, default=0.01)
    parser.add_argument("--gae-lambda", type=float, default=0.95)
    parser.add_argument("--bound-action-method", type=str, default="clip")
    parser.add_argument("--lr-decay", type=int, default=True)
    parser.add_argument("--max-grad-norm", type=float, default=0.5)
    parser.add_argument("--eps-clip", type=float, default=0.2)
    parser.add_argument("--dual-clip", type=float, default=None)
    parser.add_argument("--value-clip", type=int, default=1)
    parser.add_argument("--norm-adv", type=int, default=0)
    parser.add_argument("--recompute-adv", type=int, default=0)
    parser.add_argument("--render", type=float, default=0.)
    parser.add_argument(
        "--device", type=str, default="cuda" if torch.cuda.is_available() else "cpu"
    )
    parser.add_argument("--resume-path", type=str, default=None)
    parser.add_argument("--base-task-path", type=str, default=None)
    parser.add_argument("--resume-id", type=str, default=None)
    parser.add_argument("--log-dir", type=str,default="./mylog")
    parser.add_argument(
        "--logger",
        type=str,
        default="wandb",
        choices=["tensorboard", "wandb"],
    )
    parser.add_argument("--wandb-project", type=str, default="stabilized-rl")
    parser.add_argument("--wandb-group", type=str, default="ppo")
    parser.add_argument("--wandb-entity", type=str, default=None)
    parser.add_argument(
        "--watch",
        default=False,
        action="store_true",
        help="watch the play of pre-trained policy only",
    )
    return parser.parse_args()


def run_ppo(args=get_args()):
    print("------------------------!")
    env, train_envs, test_envs = make_metaworld_env(
        args.env, args.seed, args.training_num, args.test_num, obs_norm=True
    )
    args.state_shape = env.observation_space.shape or env.observation_space.n
    args.action_shape = env.action_space.shape or env.action_space.n
    args.max_action = env.action_space.high[0]
    print("Observations shape:", args.state_shape)
    print("Actions shape:", args.action_shape)
    print("Action range:", np.min(env.action_space.low), np.max(env.action_space.high))
    # seed
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    # model
    net_a = Net(
        args.state_shape,
        hidden_sizes=args.hidden_sizes,
        activation=nn.Tanh,
        device=args.device,
    )
    actor = ActorProb(
        net_a,
        args.action_shape,
        max_action=args.max_action,
        unbounded=True,
        device=args.device,
    ).to(args.device)
    net_c = Net(
        args.state_shape,
        hidden_sizes=args.hidden_sizes,
        activation=nn.Tanh,
        device=args.device,
    )
    critic = Critic(net_c, device=args.device).to(args.device)
    torch.nn.init.constant_(actor.sigma_param, -0.5)
    for m in list(actor.modules()) + list(critic.modules()):
        if isinstance(m, torch.nn.Linear):
            # orthogonal initialization
            torch.nn.init.orthogonal_(m.weight, gain=np.sqrt(2))
            torch.nn.init.zeros_(m.bias)
    # do last policy layer scaling, this will make initial actions have (close to)
    # 0 mean and std, and will help boost performances,
    # see https://arxiv.org/abs/2006.05990, Fig.24 for details
    for m in actor.mu.modules():
        if isinstance(m, torch.nn.Linear):
            torch.nn.init.zeros_(m.bias)
            m.weight.data.copy_(0.01 * m.weight.data)

    optim = torch.optim.Adam(
        list(actor.parameters()) + list(critic.parameters()), lr=args.lr
    )

    lr_scheduler = None
    if args.lr_decay:
        # decay learning rate to 0 linearly
        max_update_num = np.ceil(
            args.step_per_epoch / args.step_per_collect
        ) * args.epoch

        lr_scheduler = LambdaLR(
            optim, lr_lambda=lambda epoch: 1 - epoch / max_update_num
        )

    def dist(*logits):
        return Independent(Normal(*logits), 1)

    policy = PPOPolicy(
        actor,
        critic,
        optim,
        dist,
        discount_factor=args.gamma,
        gae_lambda=args.gae_lambda,
        max_grad_norm=args.max_grad_norm,
        vf_coef=args.vf_coef,
        ent_coef=args.ent_coef,
        reward_normalization=args.rew_norm,
        action_scaling=True,
        action_bound_method=args.bound_action_method,
        lr_scheduler=copy.deepcopy(lr_scheduler),
        action_space=env.action_space,
        eps_clip=args.eps_clip,
        value_clip=args.value_clip,
        dual_clip=args.dual_clip,
        advantage_normalization=args.norm_adv,
        recompute_advantage=args.recompute_adv,
    )

    # load a previous policy
    if args.base_task_path:
        ckpt = torch.load(args.base_task_path, map_location=args.device)
        policy.load_state_dict(ckpt["model"])
        train_envs.set_obs_rms(ckpt["obs_rms"])
        test_envs.set_obs_rms(ckpt["obs_rms"])
        policy.optim = torch.optim.Adam(
            list(actor.parameters()) + list(critic.parameters()), lr=args.lr
        )
        lr_scheduler = None
        if args.lr_decay:
            # decay learning rate to 0 linearly
            max_update_num = np.ceil(
                args.step_per_epoch / args.step_per_collect
            ) * args.epoch

            lr_scheduler = LambdaLR(
                policy.optim, lr_lambda=lambda epoch: 1 - epoch / max_update_num
            )
        policy.lr_scheduler = lr_scheduler
        print("Loaded agent from: ", args.base_task_path)

    # load a previous policy
    if args.resume_path:
        ckpt = torch.load(args.resume_path, map_location=args.device)
        policy.load_state_dict(ckpt["model"])
        train_envs.set_obs_rms(ckpt["obs_rms"])
        test_envs.set_obs_rms(ckpt["obs_rms"])
        print("Loaded agent from: ", args.resume_path)

    # collector
    if args.training_num > 1:
        buffer = VectorReplayBuffer(args.buffer_size, len(train_envs))
    else:
        buffer = ReplayBuffer(args.buffer_size)
    train_collector = Collector(policy, train_envs, buffer, exploration_noise=True)
    test_collector = Collector(policy, test_envs)

    # logger
    if args.logger == "wandb":
        os.environ["WANDB_RUN_GROUP"] = args.wandb_group
        logger = WandbLogger(
            save_interval=1,
            name=None,
            entity=args.wandb_entity,
            run_id=args.resume_id,
            config=args,
            project=args.wandb_project,
        )
    writer = SummaryWriter(args.log_dir)
    writer.add_text("args", str(args))
    if args.logger == "tensorboard":
        logger = TensorboardLogger(writer)
    else:  # wandb
        logger.load(writer)

    def save_best_fn(policy):
        state = {"model": policy.state_dict(), "obs_rms": train_envs.get_obs_rms()}
        torch.save(state, os.path.join(args.log_dir, "policy.pth"))

    if not args.watch:
        # trainer
        result = onpolicy_trainer(
            policy,
            train_collector,
            test_collector,
            args.epoch,
            args.step_per_epoch,
            args.repeat_per_collect,
            args.test_num,
            args.batch_size,
            step_per_collect=args.step_per_collect,
            save_best_fn=save_best_fn,
            logger=logger,
            test_in_train=False,
        )
        pprint.pprint(result)

    # Let's watch its performance!
    policy.eval()
    test_envs.seed(args.seed)
    test_collector.reset()
    result = test_collector.collect(n_episode=args.test_num, render=args.render)
    print(f'Final reward: {result["rews"].mean()}, length: {result["lens"].mean()}')


if __name__ == "__main__":
    # from metaworld.envs import ALL_V2_ENVIRONMENTS_GOAL_OBSERVABLE

    # def list_all_envs():

    #     # List MetaWorld environments
    #     print("\nMetaWorld Environments:")
    #     metaworld_envs = list(ALL_V2_ENVIRONMENTS_GOAL_OBSERVABLE.keys())
    #     for env_name in metaworld_envs:
    #         print(env_name)

    # list_all_envs()
    run_ppo()
