import socket
import time
import argparse
import re
import sys
import ipaddress
from dataclasses import dataclass
from lifxlan import GREEN, RED, Light
from collections.abc import Callable

DEFAULT_MAC_ADDRESS = ""
DEFAULT_LIFX_IP_ADDRESS = ""
DEFAULT_FORZA_IP_ADDRESS = ""
DEFAULT_FORZA_UDP_PORT = 8888

def bind[A, B](a: A | None, f: Callable[[A], B | None]) -> B | None:
    if a is None:
        return None
    try:
        return f(a)
    except Exception as e:
        print(f"Pipeline failed at {f.__name__}: {e}")
        return None

def bind2[A, B, C](a: A | None, b: B | None, f: Callable[[A, B], C | None]) -> C | None:
    if a is None or b is None:
        return None
    try:
        return f(a, b)
    except Exception as e:
        print(f"Pipeline merge failed at {f.__name__}: {e}")
        return None

@dataclass(slots=True)
class Addresses():
    lifx_mac: str
    lifx_ip: str
    forza_ip: str
    forza_port: int

@dataclass(frozen=True, slots=True)
class ForzaHdrIdx:
    throttle = 315
    brake = 316

@dataclass(slots=True)
class ForzaCar:
    throttle: float
    brake: float

@dataclass(slots=True)
class LifxLightColor:
    hue: int
    saturation: int
    brightness: int
    kelvin: int

class ForzaTelem:
    last_update: float
    player_car: ForzaCar
    FORZA_BUF_SIZE = 1024
    MIN_DATA_SIZE = 324

    def __init__(self, ip: str, port: int):
        self.ip_address = ip
        self.port = port
        self.last_update = 0
        self.player_car = ForzaCar(0.0, 0.0)

    def sock_bind(self, sock: socket.socket):
        sock.bind((self.ip_address, self.port))
        sock.setblocking(False)

    def retrieve(self, sock: socket.socket) -> bool:
        try:
            latest_data = None
            while True:
                try:
                    latest_data = sock.recvfrom(self.FORZA_BUF_SIZE)[0]
                except BlockingIOError:
                    break
            if latest_data and len(latest_data) >= ForzaTelem.MIN_DATA_SIZE:
                self.data = latest_data
                self.player_car.throttle = self.data[ForzaHdrIdx.throttle]
                self.player_car.brake = self.data[ForzaHdrIdx.brake]
                return True
        except Exception:
            pass
        return False

class LifxNeon:
    UPDATE_INTERVAL = 1.0 / 15.0

    def __init__(self, ip: str, mac: str):
        self.ip = ip
        self.mac = mac
        self.light = Light(mac, ip)

    def set_color(self, color: LifxLightColor | None):
        """fix this and dont allow to accept Optional"""
        if color is not None:
            self.light.set_color([color.hue, color.saturation, color.brightness, color.kelvin], rapid=True)

class BrakeThrottleColorAdapter:
    @staticmethod
    def calculate_ratio(value: float, value_range: float) -> int:
        return int((value / value_range) * 65535)

    @staticmethod
    def throttle_indicator(car_data: ForzaCar) -> LifxLightColor | None:
        throttle_color = LifxLightColor(*GREEN)
        throttle_color.brightness = BrakeThrottleColorAdapter.calculate_ratio(car_data.throttle, 255.0)
        return throttle_color

    @staticmethod
    def brake_indicator(car_data: ForzaCar) -> LifxLightColor | None:
        brake_color = LifxLightColor(*RED)
        brake_color.brightness = BrakeThrottleColorAdapter.calculate_ratio(car_data.brake, 255.0)
        return brake_color

    @staticmethod
    def lerp(x: int, y: int, t: float) -> int:
        return int(x + (y - x) * t)

    @staticmethod
    def mix_colors(c1: LifxLightColor, c2: LifxLightColor, t: float) -> LifxLightColor:
        t = max(0.0, min(1.0, t))
        return LifxLightColor(
                hue=BrakeThrottleColorAdapter.lerp(c1.hue, c2.hue, t),
                saturation=BrakeThrottleColorAdapter.lerp(c1.saturation, c2.saturation, t),
                brightness=max(c1.brightness, c2.brightness),
                kelvin=BrakeThrottleColorAdapter.lerp(c1.kelvin, c2.kelvin, t))

    @staticmethod
    def blend_pedals(a: LifxLightColor, b: LifxLightColor) -> LifxLightColor:
        total_brightness = a.brightness + b.brightness
        return BrakeThrottleColorAdapter.mix_colors(a, b, 0.5 if total_brightness == 0 else b.brightness / total_brightness)

def forza_light(addresses: Addresses):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    forza = ForzaTelem(addresses.forza_ip, addresses.forza_port)
    lifx = LifxNeon(addresses.lifx_ip, addresses.lifx_mac)
    forza.sock_bind(sock)
    forza.last_update = time.time()
    print("Listening for Forza Telemetry...")
    while True:
        current_time = time.time()
        if (current_time - forza.last_update) >= LifxNeon.UPDATE_INTERVAL and forza.retrieve(sock):
            throttle_color = bind(forza.player_car, BrakeThrottleColorAdapter.throttle_indicator)
            brake_color = bind(forza.player_car, BrakeThrottleColorAdapter.brake_indicator)
            final_color = bind2(throttle_color, brake_color, BrakeThrottleColorAdapter.blend_pedals)
            try:
                lifx.set_color(final_color)
            except Exception as e:
                print(f"failed with {e}")
            forza.last_update = current_time

def is_valid_mac(mac):
    pattern = r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$|^([0-9A-Fa-f]{4}\.){2}([0-9A-Fa-f]{4})$'
    return bool(re.match(pattern, mac))

def is_valid_ip(ip):
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False

def prepare_addresses(args: argparse.Namespace) -> Addresses | None:
    if is_valid_ip(args.lifx_ip_address) and is_valid_ip(args.forza_ip_address) and is_valid_mac(args.mac_address):
        return Addresses(args.mac_address, args.lifx_ip_address, args.forza_ip_address, int(args.port))
    return None

def main():
    parser = argparse.ArgumentParser(
            prog='forza_light',
            description='lighting effects for Forza Horizon 6 and the LIFX light strips; provide MAC address and ip as options or hardcode')
    parser.add_argument('-l', '--lifx_ip_address')
    parser.add_argument('-f', '--forza_ip_address')
    parser.add_argument('-m', '--mac_address')
    parser.add_argument('-p', '--port')
    args = parser.parse_args()
    packaged_addresses = prepare_addresses(args)
    if packaged_addresses is None:
        print("Invalid input")
        sys.exit(1)
    try:
        forza_light(packaged_addresses)
    except KeyboardInterrupt:
        print("\nconnections closed, goodbye")
        sys.exit(0)

if __name__ == "__main__":
    main()
