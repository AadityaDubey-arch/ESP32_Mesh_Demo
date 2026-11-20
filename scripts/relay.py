#!/usr/bin/env python3
"""
relay.py - runs on MY laptop.

    Friend's laptop  <-- Bluetooth COM port -->  relay.py  <-- USB COM port -->  ESP32

Forwards newline-terminated UTF-8 messages in both directions.
Lines from the ESP32 that start with "[ESP32]" are status messages and are only
logged, never forwarded to your friend.

Usage (PowerShell):
    py relay.py --list     # show COM ports, then edit the CONFIG block below
    py relay.py            # run the relay (Ctrl+C to stop)
"""

import logging
import socket
import sys
import threading
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial is not installed. Run:  py -m pip install pyserial")

# =========================== CONFIG (edit me) ===========================
FRIEND_LINK = "serial"        # "serial" = Bluetooth COM port, "tcp" = fallback over Wi-Fi

BT_PORT = "COM_CHANGE_ME"     # INCOMING Bluetooth COM port (see Windows Bluetooth settings)
BT_BAUD = 115200              # Bluetooth virtual ports ignore this, but keep it consistent

ESP_PORT = "COM_CHANGE_ME"    # ESP32 USB COM port (Device Manager -> Ports)
ESP_BAUD = 115200             # must match USB_BAUD in the Arduino sketch

TCP_LISTEN_HOST = "0.0.0.0"   # only used when FRIEND_LINK = "tcp"
TCP_LISTEN_PORT = 5000

ESP_SETTLE_SECONDS = 2.0      # opening the ESP32 port can reset it; wait for boot to finish
RECONNECT_DELAY = 3.0         # seconds between reconnect attempts
MAX_LINE_BYTES = 1000         # longer messages are split (ESP32 buffer is 1024)
ESP_STATUS_PREFIX = "[ESP32]"
# ========================================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("relay")
stop = threading.Event()


def explain_error(exc, port=""):
    """Turn cryptic Windows errors into plain English."""
    text = str(exc)
    if "PermissionError" in text or "Access is denied" in text:
        return (f"{text}\n    -> {port} is already open in another program "
                "(Arduino Serial Monitor, PuTTY, a second relay.py...). Close it.")
    if "FileNotFoundError" in text:
        return (f"{text}\n    -> {port} does not exist. Check the number in Device Manager / "
                "Bluetooth COM Ports, and that the device is plugged in or paired.")
    if "semaphore" in text.lower() or "timeout" in text.lower():
        return (f"{text}\n    -> Windows could not reach the Bluetooth device. "
                "Is the other laptop on, in range, and running its program?")
    return text


class Endpoint:
    """One side of the relay. Thread rules: one reader thread, writes guarded by a lock."""

    def __init__(self, name):
        self.name = name
        self.write_lock = threading.Lock()
        self.connected = threading.Event()

    def connect(self):          # must raise on failure
        raise NotImplementedError

    def read_chunk(self):       # returns b"" on timeout, raises ConnectionError on loss
        raise NotImplementedError

    def write(self, data):      # raises ConnectionError on failure
        raise NotImplementedError

    def close(self):
        self.connected.clear()


class SerialEndpoint(Endpoint):
    def __init__(self, name, port, baud, settle=0.0, avoid_reset=False):
        super().__init__(name)
        self.port, self.baud, self.settle, self.avoid_reset = port, baud, settle, avoid_reset
        self.ser = None

    def connect(self):
        ser = serial.Serial()
        ser.port = self.port
        ser.baudrate = self.baud
        ser.timeout = 0.5
        ser.write_timeout = 2
        if self.avoid_reset:            # keeps DTR/RTS low so the ESP32 is less likely to reboot
            ser.dtr = False
            ser.rts = False
        ser.open()
        if self.settle:
            time.sleep(self.settle)
            ser.reset_input_buffer()    # throw away boot-ROM noise
        with self.write_lock:
            self.ser = ser

    def read_chunk(self):
        try:
            waiting = self.ser.in_waiting
            return self.ser.read(waiting if waiting else 1)
        except (serial.SerialException, OSError, AttributeError) as exc:
            raise ConnectionError(str(exc))

    def write(self, data):
        with self.write_lock:           # only one thread writes to this port at a time
            try:
                self.ser.write(data)
                self.ser.flush()
            except (serial.SerialException, OSError, AttributeError) as exc:
                raise ConnectionError(str(exc))

    def close(self):
        self.connected.clear()
        with self.write_lock:
            try:
                if self.ser:
                    self.ser.close()
            except Exception:
                pass
            self.ser = None


class TcpServerEndpoint(Endpoint):
    """Fallback: friend's laptop connects to us over the network instead of Bluetooth."""

    def __init__(self, name, host, port):
        super().__init__(name)
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind((host, port))
        self.server.listen(1)
        self.conn = None

    def connect(self):
        self.server.settimeout(1.0)
        while not stop.is_set():
            try:
                conn, addr = self.server.accept()
            except socket.timeout:
                continue
            conn.settimeout(0.5)
            with self.write_lock:
                self.conn = conn
            log.info("[%s] friend connected from %s", self.name, addr[0])
            return
        raise ConnectionError("stopped")

    def read_chunk(self):
        try:
            data = self.conn.recv(4096)
        except socket.timeout:
            return b""
        except (OSError, AttributeError) as exc:
            raise ConnectionError(str(exc))
        if not data:
            raise ConnectionError("peer closed the connection")
        return data

    def write(self, data):
        with self.write_lock:
            try:
                self.conn.sendall(data)
            except (OSError, AttributeError) as exc:
                raise ConnectionError(str(exc))

    def close(self):
        self.connected.clear()
        with self.write_lock:
            try:
                if self.conn:
                    self.conn.close()
            except Exception:
                pass
            self.conn = None


def handle_line(raw, src, dst, direction):
    text = raw.decode("utf-8", errors="replace").strip("\r\n").strip()
    if not text:
        return
    if src.name == "ESP32" and text.startswith(ESP_STATUS_PREFIX):
        log.info("[ESP32 status] %s", text[len(ESP_STATUS_PREFIX):].strip())
        return
    if not dst.connected.is_set():
        log.warning("%s : DROPPED (%s not connected): %s", direction, dst.name, text)
        return
    try:
        dst.write((text + "\n").encode("utf-8"))
        log.info("%s : %s", direction, text)
    except ConnectionError as exc:
        log.warning("%s : DROPPED, write to %s failed: %s", direction, dst.name, exc)
        dst.close()     # its own reader thread will reconnect


def pump(src, dst, direction):
    """Reader thread: read src, split into lines, forward to dst."""
    buf = bytearray()
    last_error = None
    while not stop.is_set():
        if not src.connected.is_set():
            try:
                src.connect()
                src.connected.set()
                buf.clear()
                last_error = None
                log.info("[%s] connected", src.name)
            except Exception as exc:
                if stop.is_set():
                    break
                msg = explain_error(exc, getattr(src, "port", ""))
                if msg != last_error:           # do not spam the same error
                    log.error("[%s] cannot connect: %s  (retrying every %.0fs)",
                              src.name, msg, RECONNECT_DELAY)
                    last_error = msg
                stop.wait(RECONNECT_DELAY)
            continue
        try:
            chunk = src.read_chunk()
        except ConnectionError as exc:
            if stop.is_set():
                break
            log.error("[%s] connection lost: %s", src.name, exc)
            src.close()
            continue
        if not chunk:
            continue
        buf.extend(chunk)
        while True:
            idx = buf.find(b"\n")
            if idx < 0 and len(buf) < MAX_LINE_BYTES:
                break
            if idx < 0:                         # overlong line: split it
                line, buf = bytes(buf), bytearray()
            else:
                line = bytes(buf[:idx])
                del buf[:idx + 1]
            handle_line(line, src, dst, direction)


def list_all_ports():
    ports = list(list_ports.comports())
    if not ports:
        print("No COM ports found.")
    for p in ports:
        print(f"  {p.device:8} {p.description}")


def main():
    if "--list" in sys.argv:
        print("Available COM ports:")
        list_all_ports()
        return

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    problems = []
    if ESP_PORT.startswith("COM_CHANGE"):
        problems.append("ESP_PORT is not set")
    if FRIEND_LINK == "serial" and BT_PORT.startswith("COM_CHANGE"):
        problems.append("BT_PORT is not set")
    if FRIEND_LINK not in ("serial", "tcp"):
        problems.append('FRIEND_LINK must be "serial" or "tcp"')
    if problems:
        print("Edit the CONFIG block at the top of relay.py: " + "; ".join(problems) + ".")
        print("Ports on this computer:")
        list_all_ports()
        sys.exit(1)

    esp = SerialEndpoint("ESP32", ESP_PORT, ESP_BAUD, settle=ESP_SETTLE_SECONDS, avoid_reset=True)
    if FRIEND_LINK == "serial":
        friend = SerialEndpoint("FRIEND", BT_PORT, BT_BAUD)
    else:
        friend = TcpServerEndpoint("FRIEND", TCP_LISTEN_HOST, TCP_LISTEN_PORT)
        log.info("Waiting for friend_chat.py on TCP port %d", TCP_LISTEN_PORT)

    threads = [
        threading.Thread(target=pump, args=(friend, esp, "FRIEND -> ESP32"), daemon=True),
        threading.Thread(target=pump, args=(esp, friend, "ESP32 -> FRIEND"), daemon=True),
    ]
    for t in threads:
        t.start()
    log.info("Relay running. Press Ctrl+C to stop.")

    try:
        while any(t.is_alive() for t in threads):
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()
        log.info("Stopping...")
    finally:
        stop.set()
        friend.close()
        esp.close()
        for t in threads:
            t.join(timeout=2)
        log.info("Relay stopped.")


if __name__ == "__main__":
    main()
