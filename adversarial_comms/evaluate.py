import argparse
import collections.abc
import json
import matplotlib.pyplot as plt
import numpy as np
import os
import pandas as pd
import ray
import time
import traceback

from pathlib import Path
from ray.rllib.models import ModelCatalog
from ray.tune.logger import NoopLogger
from ray.tune.registry import register_env
from ray.util.multiprocessing import Pool

from .environments.coverage import CoverageEnv
from .environments.path_planning import PathPlanningEnv
from .models.adversarial import AdversarialModel
# from .trainers.multiagent_ppo import MultiPPOTrainer
from .trainers.multi_trainer import MultiPPOTrainer
# from .trainers.random_heuristic import RandomHeuristicTrainer

from .trainers.hom_multi_action_dist import TorchHomogeneousMultiActionDistribution
import imageio

def update_dict(d, u):
    for k, v in u.items():
        if isinstance(v, collections.abc.Mapping):
            d[k] = update_dict(d.get(k, {}), v)
        else:
            d[k] = v
    return d

def run_trial(trainer_class=MultiPPOTrainer, checkpoint_path=None, trial=0, cfg_update={}, render=False, out_path=None):
    try:
        t0 = time.time()
        cfg = {'env_config': {}, 'model': {}}
        if checkpoint_path is not None:
            # We might want to run policies that are not loaded from a checkpoint
            # (e.g. the random policy) and therefore need this to be optional
            with open(Path(checkpoint_path).parent/"params.json") as json_file:
                cfg = json.load(json_file)

        if 'evaluation_config' in cfg:
            # overwrite the environment config with evaluation one if it exists
            cfg = update_dict(cfg, cfg['evaluation_config'])

        cfg = update_dict(cfg, cfg_update)

        trainer = trainer_class(
            env=cfg['env'],
            logger_creator=lambda config: NoopLogger(config, ""),
            config={
                "framework": "torch",
                "seed": trial,
                "num_workers": 0,
                "env_config": cfg['env_config'],
                "model": cfg['model']
            }
        )
        if checkpoint_path is not None:
            # checkpoint_file = Path(checkpoint_path)/('checkpoint-'+os.path.basename(checkpoint_path).split('_')[-1])
            checkpoint_file = Path(checkpoint_path)
            trainer.restore(str(checkpoint_file))

        envs = {'coverage': CoverageEnv, 'path_planning': PathPlanningEnv}
        env = envs[cfg['env']](cfg['env_config'])
        env.seed(trial)
        obs = env.reset()

        results = []
        images = []

        for i in range(cfg['env_config']['max_episode_len']):
            actions = trainer.compute_single_action(obs)
            obs, reward, done, info = env.step(actions)
            if render:
                figure = env.render()
                image = np.frombuffer(figure.canvas.tostring_rgb(), dtype = 'uint8')
                images.append(image.reshape(600, 600, 3))
            for j, reward in enumerate(list(info['rewards'].values())):
                results.append({
                    'step': i,
                    'agent': j,
                    'trial': trial,
                    'reward': reward
                })

        print("Done", time.time() - t0)
        if out_path != None:
            image_file = path_to_hash(checkpoint_path) + '-' + str(trial) + '.gif'
            imageio.mimsave(Path(out_path) / image_file, images[1:])
    except Exception as e:
        print(e, traceback.format_exc())
        raise
    df = pd.DataFrame(results)
    return df

def path_to_hash(path):
    path_split = path.split('/')
    checkpoint_number_string = path_split[-1].split('_')[-1]
    path_hash = path_split[-2].split('_')[-2]
    return path_hash + '-' + checkpoint_number_string

def serve_config(checkpoint_path, trials, cfg_change={}, trainer=MultiPPOTrainer):
    with Pool() as p:
        results = pd.concat(p.starmap(run_trial, [(trainer, checkpoint_path, t, cfg_change) for t in range(trials)]))
    return results

def initialize():
    ray.init()
    register_env("coverage", lambda config: CoverageEnv(config))
    register_env("path_planning", lambda config: PathPlanningEnv(config))
    ModelCatalog.register_custom_model("adversarial", AdversarialModel)
    ModelCatalog.register_custom_action_dist("hom_multi_action", TorchHomogeneousMultiActionDistribution)

def eval_nocomm(env_config_func, prefix):
    parser = argparse.ArgumentParser()
    parser.add_argument("checkpoint")
    parser.add_argument("out_path")
    parser.add_argument("-t", "--trials", type=int, default=100)
    args = parser.parse_args()

    initialize()
    results = []
    for comm in [False, True]:
        cfg_change={'env_config': env_config_func(comm)}
        df = serve_config(args.checkpoint, args.trials, cfg_change=cfg_change, trainer=MultiPPOTrainer)
        df['comm'] = comm
        results.append(df)

    with open(Path(args.checkpoint).parent/"params.json") as json_file:
        cfg = json.load(json_file)
        if 'evaluation_config' in cfg:
            update_dict(cfg, cfg['evaluation_config'])

    df = pd.concat(results)
    df.attrs = cfg
    filename = prefix + "-" + path_to_hash(args.checkpoint) + ".pkl"
    df.to_pickle(Path(args.out_path)/filename)

def eval_nocomm_coop():
    # Cooperative agents can communicate or not (without comm interference from adversarial agent)
    eval_nocomm(lambda comm: {
        'disabled_teams_comms': [True, not comm],
        'disabled_teams_step': [True, False]
    }, "eval_coop")

def eval_nocomm_adv():
    # all cooperative agents can still communicate, but adversarial communication is switched
    eval_nocomm(lambda comm: {
        'disabled_teams_comms': [not comm, False], # en/disable comms for adv and always enabled for coop
        'disabled_teams_step': [False, False] # both teams operating
    }, "eval_adv")

def plot_agent(ax, df, color, experiment, max_cov, step_aggregation='sum', linestyle='-'):
    # world_shape = df.attrs['env_config']['world_shape']
    # max_cov = world_shape[0]*world_shape[1]*df.attrs['env_config']['min_coverable_area_fraction']
    
    if (experiment == 'coverage') or (experiment == 'split_coverage'):
        d = (df.sort_values(['trial', 'step']).groupby(['trial', 'step'])['reward'].apply(step_aggregation, 'step').groupby('trial').cumsum()/max_cov*100).groupby('step')
    elif experiment == 'path_planning':
        d = df.groupby(['step'])['reward']
        # d = df.groupby(by = ['step', 'trial']).sum().groupby(by = ['step'])['reward']
        # d = df.groupby(['step', 'trial']).sum()['reward'].groupby('step')
    else:
        raise NotImplementedError("Unknown experiment type", experiment)

    ax.plot(d.mean(), color=color, ls=linestyle)
    ax.fill_between(np.arange(len(d.mean())), np.clip(d.mean()-d.std(), 0, None), d.mean()+d.std(), alpha=0.1, color=color)
    print('mean: ', np.round(d.mean().iloc[-1], 2))
    print('std: ', np.round(d.std().iloc[-1], 2))

def plot():
    parser = argparse.ArgumentParser()
    parser.add_argument("data")
    parser.add_argument("-o", "--out_file", default=None)
    parser.add_argument("-e", "--experiment", default = None)
    parser.add_argument("-a", "--aggregation", default = 'sum')
    args = parser.parse_args()

    fig_overview = plt.figure(figsize=[4, 4])
    ax = fig_overview.subplots(1, 1)

    df = pd.read_pickle(args.data)

    if args.experiment == "coverage":
        aggr = args.aggregation
        ax.set_ylabel("Coverage %")
        ax.set_ylim(0, 100)
        world_shape = df.attrs['env_config']['world_shape']
        max_cov = world_shape[0] * world_shape[1] * df.attrs['env_config']['min_coverable_area_fraction']
    elif args.experiment == "split_coverage":
        aggr = args.aggregation
        ax.set_ylabel("Coverage %")
        ax.set_ylim(0, 100)
        world_shape = df.attrs['env_config']['world_shape']
        max_cov = world_shape[0] * world_shape[1] * (11 / 24)
    elif args.experiment == "path_planning":
        ax.set_ylabel("Probability for an agent to have reached its goal")
        ax.set_ylim(0, 1)
        max_cov = None
        aggr = None
    else:
        raise NotImplementedError("Unknown experiment type", args.experiment)

    if Path(args.data).name.startswith('eval_adv'):
        print('Coop agent(s), w/ adv comm:')
        plot_agent(ax, df[(df['comm'] == True) & (df['agent'] > 0)], 'b', args.experiment, max_cov, step_aggregation=aggr, linestyle='-')
        print('Coop agent(s), w/o adv comm:')
        plot_agent(ax, df[(df['comm'] == False) & (df['agent'] > 0)], 'b', args.experiment, max_cov, step_aggregation=aggr, linestyle=':')
        print('Adv agent, w/ adv comm:')
        plot_agent(ax, df[(df['comm'] == True) & (df['agent'] == 0)], 'r', args.experiment, max_cov, step_aggregation=aggr, linestyle='-')
        print('Adv agent, w/o adv comm:')
        plot_agent(ax, df[(df['comm'] == False) & (df['agent'] == 0)], 'r', args.experiment, max_cov, step_aggregation=aggr, linestyle=':')    
    elif Path(args.data).name.startswith('eval_coop'):
        print('Coop agent(s), w/ comm:')
        plot_agent(ax, df[(df['comm'] == True) & (df['agent'] > 0)], 'b', args.experiment, max_cov, step_aggregation=aggr, linestyle='-')
        print('Coop agent(s), w/o comm:')
        plot_agent(ax, df[(df['comm'] == False) & (df['agent'] > 0)], 'b', args.experiment, max_cov, step_aggregation=aggr, linestyle=':')
    elif Path(args.data).name.startswith('eval_rand'):
        plot_agent(ax, df[df['agent'] > 0], 'b', args.experiment, max_cov, step_aggregation=aggr, linestyle='-')

    ax.set_xlabel("Episode time steps")
    ax.margins(x=0, y=0)
    ax.grid()

    fig_overview.tight_layout()
    if args.out_file is not None:
        fig_overview.savefig(args.out_file, dpi=300)

    plt.show()

def serve():
    parser = argparse.ArgumentParser()
    parser.add_argument("checkpoint")
    parser.add_argument("-s", "--seed", type=int, default=0)
    parser.add_argument("-o", "--out_path", default=None)
    args = parser.parse_args()

    initialize()
    run_trial(checkpoint_path=args.checkpoint, trial=args.seed, render=True, out_path=args.out_path)

