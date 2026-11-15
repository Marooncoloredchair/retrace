"""Read logger button lines. Does not send N or D."""

import serial

port = serial.Serial()
port.dtr = False
port.rts = False
port.port = "COM5"
port.baudrate = 115200
port.timeout = 0.3
port.open()
print("bench listening", flush=True)
try:
    while True:
        line = port.readline()
        if not line:
            continue
        print(line.decode("ascii", errors="replace").rstrip(), flush=True)
finally:
    port.close()
