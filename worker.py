import copy
import os
import matplotlib.pyplot as plt

import imageio
import numpy as np
import torch
import torch.nn as nn

from env import Env
from parameter import *


class Worker:
    def __init__(self, meta_agent_id, actor_critic, global_step, save_image=False):
        # Handle devices for global training and local simulation
        self.device = torch.device('cuda') if USE_GPU_GLOBAL else torch.device('cpu')
        self.local_device = torch.device('cuda') if USE_GPU else torch.device('cpu')

        # Initialise local actor critic for simulation
        self.actor_critic = actor_critic

        # Initalise simulation environment
        self.metaAgentID = meta_agent_id
        self.global_step = global_step
        self.k_size = K_SIZE
        self.max_timestep = MAX_TIMESTEP_PER_EPISODE
        self.save_image = save_image
        self.env = Env(map_index=self.global_step, k_size=self.k_size, plot=save_image)

        # Initialise varibles
        self.travel_dist = 0
        self.robot_position = self.env.start_position  

        # Episode buffer
        self.episode_buffer = []
        self.perf_metrics = dict()
        for i in range(5):
            self.episode_buffer.append([])

    # Function to get corner coords for robot local area
    def get_local_map_boundaries(self, robot_position, local_size, full_size):
        x_center, y_center = robot_position
        local_h, local_w = local_size
        full_h, full_w = full_size
        x_start, y_start = x_center - local_w // 2, y_center - local_h // 2
        x_end, y_end = x_start + local_w, y_start + local_h

        if x_start < 0:
            x_start, x_end = 0, local_w
        if x_end >= full_w:
            x_start, x_end = full_w - local_w, full_w
        if y_start < 0:
            y_start, y_end = 0, local_h
        if y_end >= full_h:
            y_start, y_end = full_h - local_h, full_h

        local_robot_y = y_center - y_start
        local_robot_x = x_center - x_start

        return y_start, y_end, x_start, x_end, local_robot_y, local_robot_x

    # Retrieve observation with shape (8 x Local H x Local W)
    def get_observations(self):
        # observation[0, :, :] probability of obstacle
        # observation[1, :, :] probability of exploration
        # observation[2, :, :] indicator of current position
        # observation[3, :, :] indicator of visited

        # TODO make it less computationally intensive
        robot_belief = copy.deepcopy(self.env.robot_belief)
        visited_map = copy.deepcopy(self.env.visited_map)
        ground_truth_size = copy.deepcopy(self.env.ground_truth_size)  # (480, 640)
        local_size = (int(ground_truth_size[0] / MAP_DOWNSIZE_FACTOR), \
                      int(ground_truth_size[1] / MAP_DOWNSIZE_FACTOR)) # (h,w)
        
        global_map = torch.zeros(4, ground_truth_size[0], ground_truth_size[1]).to(self.local_device)
        local_map = torch.zeros(4, local_size[0], local_size[1]).to(self.local_device)
        observations = torch.zeros(8, local_size[0], local_size[1]).to(self.local_device) # (8,height,width)

        lmb = self.get_local_map_boundaries(self.robot_position, local_size, ground_truth_size)

        # Create a mask for each condition
        mask_obst = (robot_belief == 1) # if colour 1 : index 0 = 1, index 1 = 1 obst
        mask_free = (robot_belief == 255) # if colour 255: index 0 = 0, index 1 = 1 free
        mask_unkn = (robot_belief == 127) # if colour 127: index 0 = 0, index 1 = 0 unkw
        mask_visi = (visited_map == 1) # if visited: index : 3 = 1 vist

        # Update global map based on the masks
        global_map[0, mask_obst] = 1
        global_map[1, mask_obst] = 1
        global_map[0, mask_free] = 0
        global_map[1, mask_free] = 1
        global_map[0, mask_unkn] = 0
        global_map[1, mask_unkn] = 0
        global_map[3, mask_visi] = 1
        global_map[2, self.robot_position[1] - 4:self.robot_position[1] + 5, \
                    self.robot_position[0] - 4:self.robot_position[0] + 5] = 1 
        
        local_map = global_map[:, lmb[0]:lmb[1], lmb[2]:lmb[3]] # (width,height)

        observations[0:4, :, :] = local_map.detach()
        observations[4:, :, :] = nn.MaxPool2d(MAP_DOWNSIZE_FACTOR)(global_map)

        '''map check uncomment to check output of observation'''
        # fig, axes = plt.subplots(1, 3, figsize=(10, 5))
        # axes[0].imshow(robot_belief, cmap='gray')
        # axes[1].imshow(global_map[3, : :], cmap='gray') 
        # axes[2].imshow(local_map[3, : :], cmap='gray')
        # plt.savefig('output.png')
        return observations
    
    def save_observations(self, observations):
        self.episode_buffer[0].append(observations)

    def save_action(self, action, action_log_probs):
        self.episode_buffer[1].append(action)
        self.episode_buffer[2].append(action_log_probs)

    def save_reward_done(self, reward, done):        
        self.episode_buffer[3].append(torch.tensor(reward, dtype=torch.float).to(self.local_device))

    def save_return(self, episode_rewards):
        # The returns per episode per batch to return.
		# The shape will be (num timesteps per episode)
        episode_returns = []
        discounted_reward = 0 # The discounted reward so far

        # Iterate through all rewards per episode backwards
        for rew in reversed(episode_rewards):
            discounted_reward = rew + discounted_reward * GAMMA
            episode_returns.insert(0, discounted_reward)
        episode_returns = torch.tensor(episode_returns, dtype=torch.float).to(self.local_device)
        self.episode_buffer[4] = episode_returns

    # Process actor output to target position
    def find_target_pos(self, action):
        with torch.no_grad():
            post_sig_action = nn.Sigmoid()(action).cpu().numpy()
        ground_truth_size = copy.deepcopy(self.env.ground_truth_size)  # (480, 640)
        local_size = (int(ground_truth_size[0] / MAP_DOWNSIZE_FACTOR),\
                      int(ground_truth_size[1] / MAP_DOWNSIZE_FACTOR))  # (h,w)
        lmb = self.get_local_map_boundaries(self.robot_position, local_size, ground_truth_size)
        target_position = np.array([int(post_sig_action[1] * 320 + lmb[2]), int(post_sig_action[0] * 240 + lmb[0])]) # [x,y]
        return target_position

    def find_waypoint(self, target_position):
        dist_from_target = -1
        for frontier in self.env.frontiers:
            current_dist = np.linalg.norm(target_position - frontier)
            if current_dist < dist_from_target or dist_from_target == -1:
                dist_from_target = current_dist
                closest_frontier = frontier
        return closest_frontier

    def run_episode(self, curr_episode):
        done = False

        observations = self.get_observations()
        self.save_observations(observations)
        value, action, action_log_probs = self.actor_critic.act(observations)

        '''From raw action -> target pos -> waypoint
        -> waypoint node -> waypoint node pos'''
        # target_position = self.find_target_pos(action)
        # waypoint = self.find_waypoint(target_position)
        # waypoint_node_index = self.env.find_index_from_coords(waypoint)
        # waypoint_node_position = self.env.node_coords[waypoint_node_index]

        '''From raw action -> target pos -> target node -> target not pos'''
        target_position = self.find_target_pos(action)
        target_node_index = self.env.find_index_from_coords(target_position)
        target_node_position = self.env.node_coords[target_node_index]

        reward = 0

        for num_step in range(self.max_timestep):

            planning_step = num_step // NUM_ACTION_STEP
            action_step = num_step % NUM_ACTION_STEP

            # Use a star to find shortest path to target node
            dist, route = self.env.graph_generator.find_shortest_path(self.robot_position, target_node_position, self.env.node_coords)

            # Handle route given
            # If target == curent pos, remain at same spot
            # Elif target == unreachable, remain at same spot
            # NOTE can have a better way to do this, ie find closest point?
            # Else gp tp next node in path planned by astar
            if route == []: 
                next_position = self.robot_position
            elif route == None: 
                next_position = self.robot_position
            else:
                next_position = self.env.node_coords[int(route[1])]

            step_reward, done, self.robot_position, self.travel_dist = self.env.step(self.robot_position, next_position, target_position, self.travel_dist)
            reward += step_reward
            
            # save a frame
            if self.save_image:
                if not os.path.exists(gifs_path):
                    os.makedirs(gifs_path)
                self.env.plot_env(self.global_step, gifs_path, num_step, self.travel_dist)
            
            # At last action step do global selection
            if action_step == NUM_ACTION_STEP - 1 or done:
                self.save_action(action, action_log_probs)
                self.save_reward_done(reward, done)

                reward = 0

                if done or planning_step == NUM_PLANNING_STEP - 1:
                    self.save_return(self.episode_buffer[3]) # input rewards to cal return
                    break

                observations = self.get_observations()
                self.save_observations(observations)
                value, action, action_log_probs = self.actor_critic.act(observations)

                '''From raw action -> target pos -> waypoint
                -> waypoint node -> waypoint node pos'''
                # target_position = self.find_target_pos(action)
                # waypoint = self.find_waypoint(target_position)
                # waypoint_node_index = self.env.find_index_from_coords(waypoint)
                # waypoint_node_position = self.env.node_coords[waypoint_node_index]
                
                '''From raw action -> target pos -> target node -> target not pos'''
                target_position = self.find_target_pos(action)
                target_node_index = self.env.find_index_from_coords(target_position)
                target_node_position = self.env.node_coords[target_node_index]   

        # save metrics
        self.perf_metrics['travel_dist'] = self.travel_dist
        self.perf_metrics['explored_rate'] = self.env.explored_rate
        self.perf_metrics['success_rate'] = done

        # save gif
        if self.save_image:
            path = gifs_path
            self.make_gif(path, curr_episode)

    def work(self, currEpisode):
        self.run_episode(currEpisode)

    def make_gif(self, path, n):
        with imageio.get_writer('{}/{}_explored_rate_{:.4g}.gif'.format(path, n, self.env.explored_rate), mode='I', duration=0.5) as writer:
            for frame in self.env.frame_files:
                image = imageio.imread(frame)
                writer.append_data(image)
        print('gif complete\n')

        # Remove files
        for filename in self.env.frame_files[:-1]:
            os.remove(filename)