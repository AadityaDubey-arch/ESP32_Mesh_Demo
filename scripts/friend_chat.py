#!/usr/bin/env python3
"""
friend_chat.py - runs on my FRIEND'S laptop.

Simple terminal chat over the OUTGOING Bluetooth COM port (or TCP fallback).
Type a message and press Enter to send. Type /quit to exit.

Usage (PowerShell):
    py friend_chat.py --list    # show COM ports, then edit the CONFIG block below
    py friend_chat.py
"""

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
LINK = "serial"               # "serial" = Bluetooth COM port, "tcp" = fallback over Wi-Fi

BT_PORT = "COM_CHANGE_ME"     # OUTGOING Bluetooth COM port on THIS laptop
BT_BAUD = 115200              # Bluetooth virtual ports ignore this, but keep it consistent

TCP_HOST = "RELAY_LAPTOP_IP"  # only for LINK = "tcp": the IP address of the relay laptop
TCP_PORT = 5000               # must match TCP_LISTEN_PORT in relay.py

RECONNECT_DELAY = 3.0
MAX_LINE_BYTES = 1000
# ========================================================================

stop = threading.Event()
connected = threading.Event()
print_lock = threading.Lock()


def say(text):
    """Print an incoming/system line without wrecking the prompt too badly."""
    with print_lock:
        print("\r" + text + " " * 10)
        print("> ", end="", flush=True)


def explain_error(exc):
    text = str(exc)
    if "PermissionError" in text or "Access is denied" in text:
        return (f"{text}\n    -> This COM port is already open in another program. Close it.")
    if "FileNotFoundError" in text:
        return (f"{text}\n    -> This COM port does not exist. Check Bluetooth settings -> COM Ports "
                "and Device Manager.")
    if "semaphore" in text.lower() or "timeout" in text.lower():
        return (f"{text}\n    -> Windows could not reach the other laptop. Is relay.py running there "
                "(it must open the INCOMING port first), and are both laptops paired and in range?")
    return text


class SerialLink:
    def __init__(self, port, baud):
        self.port, self.baud = port, baud
        self.ser = None
        self.lock = threading.Lock()

    def connect(self):
        ser = serial.Serial()
        ser.port, ser.baudrate = self.port, self.baud
        ser.timeout = 0.5
        ser.write_timeout = 2
        ser.open()                      # for an outgoing BT port this starts the connection
        with self.lock:
            self.ser = ser

    def read_chunk(self):
        try:
            waiting = self.ser.in_waiting
            return self.ser.read(waiting if waiting else 1)
        except (serial.SerialException, OSError, AttributeError) as exc:
            raise ConnectionError(str(exc))

    def write(self, data):
        with self.lock:
            try:
                self.ser.write(data)
                self.ser.flush()
            except (serial.SerialException, OSError, AttributeError) as exc:
                raise ConnectionError(str(exc))

    def close(self):
        connected.clear()
        with self.lock:
            try:
                if self.ser:
                    self.ser.close()
            except Exception:
                pass
            self.ser = None


class TcpLink:
    def __init__(self, host, port):
        self.host, self.port = host, port
        self.sock = None
        self.lock = threading.Lock()

    def connect(self):
        sock = socket.create_connection((self.host, self.port), timeout=5)
        sock.settimeout(0.5)
        with self.lock:
            self.sock = sock

    def read_chunk(self):
        try:
            data = self.sock.recv(4096)
        except socket.timeout:
            return b""
        except (OSError, AttributeError) as exc:
            raise ConnectionError(str(exc))
        if not data:
            raise ConnectionError("the relay closed the connection")
        return data

    def write(self, data):
        with self.lock:
            try:
                self.sock.sendall(data)
            except (OSError, AttributeError) as exc:
                raise ConnectionError(str(exc))

    def close(self):
        connected.clear()
        with self.lock:
            try:
                if self.sock:
                    self.sock.close()
            except Exception:
                pass
            self.sock = None


def reader_loop(link):
    """Connects (and reconnects), then prints every incoming line."""
    buf = bytearray()
    last_error = None
    while not stop.is_set():
        if not connected.is_set():
            try:
                link.connect()
                connected.set()
                buf.clear()
                last_error = None
                say("[connected]")
            except Exception as exc:
                msg = explain_error(exc)
                if msg != last_error:
                    say(f"[cannot connect] {msg}\n[retrying every {RECONNECT_DELAY:.0f}s]")
                    last_error = msg
                stop.wait(RECONNECT_DELAY)
            continue
        try:
            chunk = link.read_chunk()
        except ConnectionError as exc:
            if not stop.is_set():
                say(f"[connection lost] {exc}")
            link.close()
            continue
        if not chunk:
            continue
        buf.extend(chunk)
        while True:
            idx = buf.find(b"\n")
            if idx < 0 and len(buf) < MAX_LINE_BYTES:
                break
            if idx < 0:
                line, buf = bytes(buf), bytearray()
            else:
                line = bytes(buf[:idx])
                del buf[:idx + 1]
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                say(f"< {text}")


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

    if LINK == "serial" and BT_PORT.startswith("COM_CHANGE"):
        print("Edit the CONFIG block at the top of friend_chat.py (BT_PORT).")
        print("Ports on this computer:")
        list_all_ports()
        sys.exit(1)
    if LINK == "tcp" and TCP_HOST == "RELAY_LAPTOP_IP":
        print("Edit the CONFIG block at the top of friend_chat.py (TCP_HOST).")
        sys.exit(1)
    if LINK not in ("serial", "tcp"):
        sys.exit('LINK must be "serial" or "tcp"')

    link = SerialLink(BT_PORT, BT_BAUD) if LINK == "serial" else TcpLink(TCP_HOST, TCP_PORT)
    threading.Thread(target=reader_loop, args=(link,), daemon=True).start()

    print("friend_chat - type a message and press Enter. Type /quit to exit.")
    print("Connecting (a Bluetooth COM port can take 10+ seconds)...")

    try:
        while not stop.is_set():
            try:
                text = input("> ").strip()
            except EOFError:
                break
            if not text:
                continue
            if text.lower() == "/quit":
                break
            if not connected.is_set():
                print("[not connected yet - message NOT sent]")
                continue
            data = text.encode("utf-8")
            if len(data) > MAX_LINE_BYTES:
                print(f"[message too long: {len(data)} bytes, limit {MAX_LINE_BYTES}]")
                continue
            try:
                link.write(data + b"\n")
            except ConnectionError as exc:
                print(f"[send failed] {exc}")
                link.close()
    except KeyboardInterrupt:
        print()
    finally:
        stop.set()
        link.close()
        time.sleep(0.2)
        print("Bye.")


if __name__ == "__main__":
    main()
