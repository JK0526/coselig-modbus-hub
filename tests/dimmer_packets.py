"""Legacy dimmer packet assertions routed through the production profile codec."""

from custom_components.coselig_modbus_hub.profiles import PROFILES, Model, write_dimmer
from custom_components.coselig_modbus_hub.protocol import read_holding_registers


def read(slave, count):
    return read_holding_registers(slave, PROFILES[Model.P404].poll.start, count)


def write(slave, register, value):
    return write_dimmer(slave, register, value)
