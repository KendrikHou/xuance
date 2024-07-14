from tqdm import tqdm
import numpy as np
from copy import deepcopy
from typing import Optional
from argparse import Namespace
from xuance.torch.agents.base import Agent
from xuance.environment import DummyVecEnv
from xuance.common import DummyOffPolicyBuffer, DummyOffPolicyBuffer_Atari


class OffPolicyAgent(Agent):
    """The core class for on-policy algorithm with single agent.

    Args:
        config: the Namespace variable that provides hyper-parameters and other settings.
        envs: the vectorized environments.
    """
    def __init__(self,
                 config: Namespace,
                 envs: DummyVecEnv):
        super(OffPolicyAgent, self).__init__(config, envs)
        self.start_greedy = config.start_greedy if hasattr(config, "start_greedy") else None
        self.end_greedy = config.end_greedy if hasattr(config, "start_greedy") else None
        self.start_noise = config.start_noise if hasattr(config, "start_noise") else None
        self.end_noise = config.end_noise if hasattr(config, "end_noise") else None
        self.auxiliary_info_shape = None
        self.memory: Optional[DummyOffPolicyBuffer] = None
        self.e_greedy: Optional[float] = None
        self.delta_egreedy: Optional[float] = None
        self.noise_scale: Optional[float] = None
        self.delta_noise: Optional[float] = None

    def _build_memory(self, auxiliary_info_shape=None):
        self.atari = True if self.config.env_name == "Atari" else False
        Buffer = DummyOffPolicyBuffer_Atari if self.atari else DummyOffPolicyBuffer
        input_buffer = dict(observation_space=self.observation_space,
                            action_space=self.action_space,
                            auxiliary_shape=auxiliary_info_shape,
                            n_envs=self.n_envs,
                            buffer_size=self.config.buffer_size,
                            batch_size=self.config.batch_size)
        return Buffer(**input_buffer)

    def _build_policy(self):
        raise NotImplementedError

    def _update_explore_factor(self):
        if self.e_greedy is not None:
            if self.e_greedy > self.end_greedy:
                self.e_greedy -= self.delta_egreedy
        elif self.noise_scale is not None:
            if self.noise_scale >= self.end_noise:
                self.noise_scale -= self.delta_noise

    def exploration(self, pi_actions):
        """Returns the actions for exploration.

        Parameters:
            pi_actions: The original output actions.

        Returns:
            explore_actions: The actions with noisy values.
        """
        random_actions = np.random.choice(self.action_space.n, self.n_envs)
        if np.random.rand() < self.e_greedy:
            actions = random_actions
        else:
            actions = pi_actions.detach().cpu().numpy()
        return actions

    def action(self, observations: np.ndarray,
               test_mode: Optional[bool] = False):
        """Returns actions and values.

        Parameters:
            observations (np.ndarray): The observation.
            test_mode (Optional[bool]): True for testing without noises.

        Returns:
            actions: The actions to be executed.
            values: The evaluated values.
            dists: The policy distributions.
            log_pi: Log of stochastic actions.
        """
        _, actions_output, _ = self.policy(observations)
        if test_mode:
            actions = self.exploration(actions_output)
        else:
            actions = actions_output.detach().cpu().numpy()
        return {"actions": actions}

    def get_aux_info(self, policy_output: dict = None):
        """Returns auxiliary information.

        Parameters:
            policy_output (dict): The output information of the policy.

        Returns:
            aux_info (dict): The auxiliary information.
        """
        return None

    def train_epochs(self, n_epochs=1):
        train_info = {}
        for _ in range(n_epochs):
            samples = self.memory.sample()
            train_info = self.learner.update(**samples)
        if self.e_greedy is not None:
            train_info["epsilon-greedy"] = self.e_greedy
        elif self.noise_scale is not None:
            train_info["noise_scale"] = self.noise_scale
        else:
            pass
        return train_info

    def train(self, train_steps):
        obs = self.envs.buf_obs
        for _ in tqdm(range(train_steps)):
            step_info = {}
            self.obs_rms.update(obs)
            obs = self._process_observation(obs)
            policy_out = self.action(obs, test_mode=False)
            acts = policy_out['actions']
            next_obs, rewards, terminals, trunctions, infos = self.envs.step(acts)

            self.memory.store(obs, acts, self._process_reward(rewards), terminals, self._process_observation(next_obs))
            if self.current_step > self.start_training and self.current_step % self.training_frequency == 0:
                step_info = self.train_epochs(n_epochs=self.n_epochs)
                self.log_infos(step_info, self.current_step)

            obs = deepcopy(next_obs)
            for i in range(self.n_envs):
                if terminals[i] or trunctions[i]:
                    if self.atari and (~trunctions[i]):
                        pass
                    else:
                        obs[i] = infos[i]["reset_obs"]
                        self.envs.buf_obs[i] = obs[i]
                        self.current_episode[i] += 1
                        if self.use_wandb:
                            step_info["Episode-Steps/env-%d" % i] = infos[i]["episode_step"]
                            step_info["Train-Episode-Rewards/env-%d" % i] = infos[i]["episode_score"]
                        else:
                            step_info["Episode-Steps"] = {"env-%d" % i: infos[i]["episode_step"]}
                            step_info["Train-Episode-Rewards"] = {"env-%d" % i: infos[i]["episode_score"]}
                        self.log_infos(step_info, self.current_step)

            self.current_step += self.n_envs
            if self.e_greedy >= self.end_greedy:
                self.e_greedy -= self.delta_egreedy

    def test(self, env_fn, test_episodes):
        test_envs = env_fn()
        num_envs = test_envs.num_envs
        videos, episode_videos = [[] for _ in range(num_envs)], []
        current_episode, scores, best_score = 0, [], -np.inf
        obs, infos = test_envs.reset()
        if self.config.render_mode == "rgb_array" and self.render:
            images = test_envs.render(self.config.render_mode)
            for idx, img in enumerate(images):
                videos[idx].append(img)

        while current_episode < test_episodes:
            self.obs_rms.update(obs)
            obs = self._process_observation(obs)
            policy_out = self.action(obs, test_mode=True)
            next_obs, rewards, terminals, trunctions, infos = test_envs.step(policy_out['actions'])
            if self.config.render_mode == "rgb_array" and self.render:
                images = test_envs.render(self.config.render_mode)
                for idx, img in enumerate(images):
                    videos[idx].append(img)

            obs = deepcopy(next_obs)
            for i in range(num_envs):
                if terminals[i] or trunctions[i]:
                    if self.atari and (~trunctions[i]):
                        pass
                    else:
                        obs[i] = infos[i]["reset_obs"]
                        scores.append(infos[i]["episode_score"])
                        current_episode += 1
                        if best_score < infos[i]["episode_score"]:
                            best_score = infos[i]["episode_score"]
                            episode_videos = videos[i].copy()
                        if self.config.test_mode:
                            print("Episode: %d, Score: %.2f" % (current_episode, infos[i]["episode_score"]))

        if self.config.render_mode == "rgb_array" and self.render:
            # time, height, width, channel -> time, channel, height, width
            videos_info = {"Videos_Test": np.array([episode_videos], dtype=np.uint8).transpose((0, 1, 4, 2, 3))}
            self.log_videos(info=videos_info, fps=self.fps, x_index=self.current_step)

        if self.config.test_mode:
            print("Best Score: %.2f" % (best_score))

        test_info = {
            "Test-Episode-Rewards/Mean-Score": np.mean(scores),
            "Test-Episode-Rewards/Std-Score": np.std(scores)
        }
        self.log_infos(test_info, self.current_step)

        test_envs.close()

        return scores

