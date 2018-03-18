from enum import Enum

from discord import InvalidArgument
from discord.ext.commands import BotMissingPermissions

from .exceptions import IllegalAction
from .node import Node
from .player import Player
from .load_balancing import LoadBalancer


class State(Enum):
    NOT_CONNECTED = 0
    CONNECTING = 1
    CONNECTED = 2
    DISCONNECTING = 3
    DESTROYING = 4
    DESTROYED = 5


class Lavalink:
    def __init__(self, bot):
        self.bot = bot
        self.loop = self.bot.loop
        self.user_id = bot.user.id
        self.shard_count = bot.shard_count
        self.load_balancer = LoadBalancer(self)
        self.nodes = {}
        self.links = {}

        self.bot.add_listener(self.on_voice_update)

    async def on_voice_update(self, data):
        if not data.get("t") in ("VOICE_SERVER_UPDATE", "VOICE_STATE_UPDATE"):
            return
        link = self.links.get(str(data['d']['guild_id']))
        await link.update_voice(data)

    def get_link(self, guild_id):
        if not guild_id in self.links:
            self.links[guild_id] = Link(self, guild_id)
        return self.links[guild_id]

    async def add_node(self, name, uri, password):
        headers = {
            "Authorization": password,
            "Num-Shards": self.shard_count,
            "User-Id": self.user_id
        }

        node = Node(self, name, uri, headers)
        await node.connect()
        self.nodes[name] = node

    async def get_best_node(self, guild):
        return self.load_balancer.determine_best_node(guild)


class Link:
    def __init__(self, lavalink, guild):
        self.lavalink = lavalink
        self.bot = self.lavalink.bot
        self.guild = guild
        self.state = State.NOT_CONNECTED
        self.last_voice_update = {}
        self._player = None
        self.node = None

    @property
    def player(self):
        if not self._player:
            self._player = Player(self)
        return self._player

    def set_state(self, state):
        if self.state.value > 3 and state.value != 5:
            raise IllegalAction(f"Cannot change the state to {state} when the state is {self.state}")
        self.state = state

    async def update_voice(self, data):
        if not self.guild:  # is this even necessary? :thinking:
            raise IllegalAction("Attempted to start audio connection with a guild that doesn't exist")

        if data["t"] == "VOICE_SERVER_UPDATE":
            self.last_voice_update.update({
                "op": "voiceUpdate",
                "event": data["d"],
                "guildId": data["d"]["guild_id"],
                "sessionId": self.guild.me.voice.session_id
            })
            await self.get_node(True).send(self.last_voice_update)
            self.set_state(State.CONNECTED)
        else:  # data["t"] == "VOICE_STATE_UPDATE"

            # We're selfish and only care about ourselves
            if int(data["d"]["user_id"]) != self.bot.user.id:
                return

            channel = self.guild.get_channel(int(data["d"]))
            if not channel and self.state != State.DESTROYED:
                self.state = State.NOT_CONNECTED
                if self.node:
                    payload = {
                        "op": "destroy",
                        "guildId": data["d"]["guildId"]
                    }
                    await self.node.send(payload)

    async def get_node(self, select_if_absent=False):
        if select_if_absent and not self.node:
            self.node = self.lavalink.get_best_node(self.guild)
            if self.player:
                await self.player.node_changed()
        return self.node

    async def change_node(self, node):
        self.node = node
        if self.last_voice_update:
            await node.send(self.last_voice_update)
            self.player.node_changed()

    async def connect(self, channel):
        # We're using discord's websocket, no lavalink
        if not channel.guild == self.guild:
            raise InvalidArgument("The guild of the channel isn't the the same as the link's!")
        if channel.guild.unavailable:
            raise IllegalAction("Cannot connect to guild that is unavailable!")

        me = channel.guild.me
        permissions = me.permissions_in(channel)
        if not permissions.connect and not permissions.move_members:
            raise BotMissingPermissions(permissions.connect)

        self.set_state(State.CONNECTING)
        payload = {
            "op": 4,
            "d": {
                "guild_id": channel.guild.id,
                "channel_id": str(channel.id),
                "self_mute": False,
                "self_deaf": False
            }
        }

        await self.bot._connection._get_websocket(channel.guild.id).send_as_json(payload)

    async def disconnect(self):
        # We're using discord's websocket, no lavalink
        payload = {
            "op": 4,
            "d": {
                "guild_id": self.guild.id,
                "channel_id": None,
                "self_mute": False,
                "self_deaf": False
            }
        }

        self.set_state(State.DISCONNECTING)
        await self.bot._connection._get_websocket(self.guild.id).send_as_json(payload)