import copy
import logging
import time

import hydra

import torch
import torch.multiprocessing as mp

import rlmeta.envs.gym_wrappers as gym_wrappers
import rlmeta.utils.hydra_utils as hydra_utils
import rlmeta.utils.remote_utils as remote_utils

from rlmeta.agents.agent import AgentFactory
from rlmeta.agents.ppo.ppo_agent import PPOAgent
from rlmeta.core.controller import Phase, Controller
from rlmeta.core.loop import LoopList, ParallelLoop
from rlmeta.core.model import wrap_downstream_model
from rlmeta.core.replay_buffer import ReplayBuffer, make_remote_replay_buffer
from rlmeta.core.server import Server, ServerList
from rlmeta.core.callbacks import EpisodeCallbacks
from rlmeta.core.types import Action, TimeStep

from cache_env_wrapper import CacheEnvCCHunterWrapperFactory
from cache_ppo_transformer_model import CachePPOTransformerModel
from cache_ppo_transformer_periodic_model \
        import CachePPOTransformerPeriodicModel
from cache_ppo_model import CachePPOModel
from metric_callbacks import CCHunterMetricCallbacks


@hydra.main(config_path="./config", config_name="ppo_cchunter")
def main(cfg):
    my_callbacks = CCHunterMetricCallbacks()
    logging.info(hydra_utils.config_to_json(cfg))

    env_fac = CacheEnvCCHunterWrapperFactory(cfg.env_config)
    env = env_fac(0)
    cfg.model_config["output_dim"] = env.action_space.n

    # train_model = CachePPOTransformerModel(**cfg.model_config).to(
    #     cfg.train_device)
    train_model = CachePPOTransformerPeriodicModel(**cfg.model_config).to(
        cfg.train_device)
    # train_model = CachePPOModel(**cfg.model_config).to(
    #     cfg.train_device)
    optimizer = torch.optim.Adam(train_model.parameters(), lr=cfg.lr)

    infer_model = copy.deepcopy(train_model).to(cfg.infer_device)

    ctrl = Controller()
    rb = ReplayBuffer(cfg.replay_buffer_size)

    m_server = Server(cfg.m_server_name, cfg.m_server_addr)
    r_server = Server(cfg.r_server_name, cfg.r_server_addr)
    c_server = Server(cfg.c_server_name, cfg.c_server_addr)
    m_server.add_service(infer_model)
    r_server.add_service(rb)
    c_server.add_service(ctrl)
    servers = ServerList([m_server, r_server, c_server])

    a_model = wrap_downstream_model(train_model, m_server)
    t_model = remote_utils.make_remote(infer_model, m_server)
    e_model = remote_utils.make_remote(infer_model, m_server)

    a_ctrl = remote_utils.make_remote(ctrl, c_server)
    t_ctrl = remote_utils.make_remote(ctrl, c_server)
    e_ctrl = remote_utils.make_remote(ctrl, c_server)

    a_rb = make_remote_replay_buffer(rb, r_server, prefetch=cfg.prefetch)
    t_rb = make_remote_replay_buffer(rb, r_server)

    agent = PPOAgent(a_model,
                     replay_buffer=a_rb,
                     controller=a_ctrl,
                     optimizer=optimizer,
                     batch_size=cfg.batch_size,
                     learning_starts=cfg.get("learning_starts", None),
                     push_every_n_steps=cfg.push_every_n_steps)
    t_agent_fac = AgentFactory(PPOAgent, t_model, replay_buffer=t_rb)
    e_agent_fac = AgentFactory(PPOAgent, e_model, deterministic_policy=True)

    t_loop = ParallelLoop(env_fac,
                          t_agent_fac,
                          t_ctrl,
                          running_phase=Phase.TRAIN,
                          should_update=True,
                          num_rollouts=cfg.num_train_rollouts,
                          num_workers=cfg.num_train_workers,
                          seed=cfg.train_seed,
                          episode_callbacks=my_callbacks)
    e_loop = ParallelLoop(env_fac,
                          e_agent_fac,
                          e_ctrl,
                          running_phase=Phase.EVAL,
                          should_update=False,
                          num_rollouts=cfg.num_eval_rollouts,
                          num_workers=cfg.num_eval_workers,
                          seed=cfg.eval_seed,
                          episode_callbacks=my_callbacks)
    loops = LoopList([t_loop, e_loop])

    servers.start()
    loops.start()
    agent.connect()

    start_time = time.perf_counter()
    for epoch in range(cfg.num_epochs):
        stats = agent.train(cfg.steps_per_epoch)
        cur_time = time.perf_counter() - start_time
        info = f"T Epoch {epoch}"
        if cfg.table_view:
            logging.info("\n\n" + stats.table(info, time=cur_time) + "\n")
        else:
            logging.info(
                stats.json(info, phase="Train", epoch=epoch, time=cur_time))
        time.sleep(1)

        stats = agent.eval(cfg.num_eval_episodes)
        cur_time = time.perf_counter() - start_time
        info = f"E Epoch {epoch}"
        if cfg.table_view:
            logging.info("\n\n" + stats.table(info, time=cur_time) + "\n")
        else:
            logging.info(
                stats.json(info, phase="Eval", epoch=epoch, time=cur_time))
        time.sleep(1)

        torch.save(train_model.state_dict(), f"ppo_agent-{epoch}.pth")

    loops.terminate()
    servers.terminate()


if __name__ == "__main__":
    mp.set_start_method("spawn")
    main()
