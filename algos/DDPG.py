import copy
import torch
import torch.nn as nn
import torch.nn.functional as F

print(torch.cuda.is_available(), torch.backends.cudnn.enabled)

# Re-tuned version of Deep Deterministic Policy Gradients (DDPG)
# Paper: https://arxiv.org/abs/1509.02971


class Actor(nn.Module):
    def __init__(
        self, lidar_state_dim, position_state_dim, lidar_feature_dim, action_dim, hidden_dim, max_action, is_recurrent=True
    ):
        super(Actor, self).__init__()
        self.recurrent = is_recurrent

        self.lidar_compress_net = nn.Sequential(
            nn.Linear(lidar_state_dim, hidden_dim), nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim), nn.ReLU(),
            nn.Linear(hidden_dim, lidar_feature_dim, nn.ReLU())
        )

        if self.recurrent:
            self.l1 = nn.LSTM(lidar_feature_dim + position_state_dim, hidden_dim, batch_first=True)
        else:
            self.l1 = nn.Linear(lidar_feature_dim + position_state_dim, hidden_dim)

        self.l2 = nn.Linear(hidden_dim, hidden_dim)
        self.l3 = nn.Linear(hidden_dim, action_dim)

        self.max_action = max_action

    def forward(self, lidar_state, position_state):
        with torch.no_grad():
            lidar_feature = self.lidar_compress_net(lidar_state)  #1800 -》50
        state = torch.cat((lidar_feature, position_state), dim=-1)

        a = F.relu(self.l1(state))

        a = F.relu(self.l2(a))
        a = torch.tanh(self.l3(a))
        return self.max_action * a


class Critic(nn.Module):
    def __init__(
        self, lidar_state_dim, position_state_dim, lidar_feature_dim, action_dim, hidden_dim, is_recurrent=True
    ):
        super(Critic, self).__init__()
        self.recurrent = is_recurrent

        self.lidar_compress_net = nn.Sequential(
            nn.Linear(lidar_state_dim, hidden_dim), nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim), nn.ReLU(),
            nn.Linear(hidden_dim, lidar_feature_dim, nn.ReLU())
        )

        # Q1 architecture
        if self.recurrent:
            self.l1 = nn.LSTM(
                lidar_feature_dim + position_state_dim + action_dim, hidden_dim, batch_first=True)
        else:
            self.l1 = nn.Linear(lidar_feature_dim + position_state_dim + action_dim, hidden_dim)

        self.l2 = nn.Linear(hidden_dim, hidden_dim)
        self.l3 = nn.Linear(hidden_dim, 1)

    def forward(self, lidar_state, position_state, action):
        lidar_feature = self.lidar_compress_net(lidar_state)
        state = torch.cat((lidar_feature, position_state), -1)
        sa = torch.cat([state, action], -1)

        q1 = F.relu(self.l1(sa))

        q1 = F.relu(self.l2(q1))
        q1 = self.l3(q1)

        return q1


class DDPG(object):
    def __init__(
        self,
        lidar_state_dim,
        position_state_dim,
        lidar_feature_dim,
        action_dim,
        max_action,
        hidden_dim,
        discount=0.99,
        tau=0.005,
        lr=3e-4,
        recurrent_actor=False,
        recurrent_critic=False,
        device='gpu'
    ):
        self.device = torch.device(device)
        self.on_policy = False
        self.recurrent = recurrent_actor
        self.actor = Actor(
            lidar_state_dim, position_state_dim, lidar_feature_dim, action_dim, hidden_dim, max_action,
            is_recurrent=recurrent_actor
        ).to(self.device)
        self.actor_target = copy.deepcopy(self.actor)
        self.actor_optimizer = torch.optim.Adam(self.actor.parameters(), lr=lr)

        self.critic = Critic(
            lidar_state_dim, position_state_dim, lidar_feature_dim, action_dim, hidden_dim,
            is_recurrent=recurrent_critic
        ).to(self.device)
        self.critic_target = copy.deepcopy(self.critic)
        self.critic_optimizer = torch.optim.Adam(self.critic.parameters(), lr=lr)

        self.discount = discount
        self.tau = tau

    def get_initial_states(self):
        h_0, c_0 = None, None
        if self.actor.recurrent:
            h_0 = torch.zeros((
                self.actor.l1.num_layers,
                1,
                self.actor.l1.hidden_size),
                dtype=torch.float)
            h_0 = h_0.to(device=self.device)

            c_0 = torch.zeros((
                self.actor.l1.num_layers,
                1,
                self.actor.l1.hidden_size),
                dtype=torch.float)
            c_0 = c_0.to(device=self.device)
        return (h_0, c_0)

    def select_action(self, lidar_state, position_state, test=True):
        lidar_state = torch.FloatTensor(
            lidar_state.reshape(1, -1)).to(self.device)[:, None, :]
        position_state = torch.FloatTensor(
            position_state.reshape(1, -1)).to(self.device)[:, None, :]

        action = self.actor(lidar_state, position_state)
        return action.cpu().data.numpy().flatten()

    def train(self, replay_buffer, batch_size=100):

        # Sample replay buffer
        lidar_state, position_state, action, next_lidar_state, next_position_state, reward, not_done = \
            replay_buffer.sample(batch_size)

        # Compute the target Q value
        target_Q = self.critic_target(
            next_lidar_state, next_position_state,
            self.actor_target(next_lidar_state, next_position_state))
        target_Q = reward + (not_done * self.discount * target_Q).detach()

        # Get current Q estimate
        current_Q = self.critic(lidar_state, position_state, action)

        # Compute critic loss
        critic_loss = F.mse_loss(current_Q, target_Q)

        # Optimize the critic
        self.critic_optimizer.zero_grad()
        critic_loss.backward()
        self.critic_optimizer.step()

        # Compute actor loss
        actor_loss = -self.critic(
            lidar_state.detach(), position_state.detach(), self.actor(lidar_state.detach(), position_state.detach())).mean()

        # Optimize the actor
        self.actor_optimizer.zero_grad()
        actor_loss.backward()
        self.actor_optimizer.step()

        # Update the frozen target models
        for param, target_param in zip(
            self.critic.parameters(), self.critic_target.parameters()
        ):
            target_param.data.copy_(
                self.tau * param.data + (1 - self.tau) * target_param.data)

        for param, target_param in zip(
            self.actor.parameters(), self.actor_target.parameters()
        ):
            target_param.data.copy_(
                self.tau * param.data + (1 - self.tau) * target_param.data)

    def save(self, filename):
        torch.save(self.critic.state_dict(), filename + "_critic")
        torch.save(self.critic_optimizer.state_dict(),
                   filename + "_critic_optimizer")
        torch.save(self.actor.state_dict(), filename + "_actor")
        torch.save(self.actor_optimizer.state_dict(),
                   filename + "_actor_optimizer")

    def load(self, filename):
        self.critic.load_state_dict(torch.load(filename + "_critic", map_location='cuda:0'))
        self.critic_optimizer.load_state_dict(
            torch.load(filename + "_critic_optimizer", map_location='cuda:0'))
        self.actor.load_state_dict(torch.load(filename + "_actor", map_location='cuda:0'))
        self.actor_optimizer.load_state_dict(
            torch.load(filename + "_actor_optimizer", map_location='cuda:0'))


    def eval_mode(self):
        self.actor.eval()
        self.critic.eval()

    def train_mode(self):
        self.actor.train()
        self.critic.train()
