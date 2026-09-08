"""Single source of channel capabilities and validation rules."""
from dataclasses import dataclass
from .protocol import write


@dataclass(frozen=True)
class Channel:
    model: str
    slave: int
    channel: str
    kind: str
    name: str
    minimum: int = 2
    mired_min: int = 175
    mired_max: int = 455

    def __post_init__(self):
        allowed = {'p404': {'1','2','3','4','a','b'}, 'p210': {'1','2'}, 'U4': {'1'}}
        if self.channel not in allowed.get(self.model, set()):
            raise ValueError('Unsupported model/channel')
        if self.kind != ('dual' if self.channel in ('a','b') else 'single'):
            raise ValueError('Channel type mismatch')
        if not 1 <= self.slave <= 247 or not 1 <= self.minimum <= 100:
            raise ValueError('Invalid slave/brightness')
        if not 0 < self.mired_min < self.mired_max:
            raise ValueError('Invalid color temperature range')

    @property
    def registers(self):
        return {'1': (2090,), '2': (2091,), '3': (2092,), '4': (2093,),
                'a': (2090,2091), 'b': (2092,2093)}[self.channel]

    @property
    def uid(self):
        return f'{self.kind}_{self.slave}_{self.channel}'

    @property
    def topic(self):
        return f'homeassistant/light/{self.kind}/{self.slave}/{self.channel}'

    def brightness_command(self, value):
        if type(value) is not int or not 0 <= value <= 100:
            raise ValueError('Brightness must be an integer 0..100')
        return write(self.slave, self.registers[0], max(self.minimum, value) if value else 0)

    def temperature_command(self, mired):
        if self.kind != 'dual' or not self.mired_min <= mired <= self.mired_max:
            raise ValueError('Invalid color temperature')
        raw = round((self.mired_max - mired) * 100 / (self.mired_max - self.mired_min))
        return write(self.slave, self.registers[1], raw)

    def states(self, registers):
        value = registers[self.registers[0] - 2090] & 255
        if not 0 <= value <= 100:
            raise ValueError('Invalid raw brightness')
        result = {f'{self.topic}/state': 'ON' if value else 'OFF',
                  f'{self.topic}/brightness': str(value)}
        if self.kind == 'dual':
            raw = registers[self.registers[1] - 2090] & 255
            if not 0 <= raw <= 100:
                raise ValueError('Invalid raw color temperature')
            result[f'{self.topic}/colortemp'] = str(round(self.mired_max - raw / 100 * (self.mired_max-self.mired_min)))
        return result


def validate_channels(channels):
    occupied = set()
    models = {}
    for channel in channels:
        if models.setdefault(channel.slave, channel.model) != channel.model:
            raise ValueError('Conflicting device model')
        for register in channel.registers:
            key = channel.slave, register
            if key in occupied:
                raise ValueError('Overlapping channels')
            occupied.add(key)
