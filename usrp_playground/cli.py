import sys
import json
import asyncio
import argparse
import websockets


async def run(server, token, input_path, output_path,
              channel=0, listen=None, channels=1):
    url = f"ws://{server}/ws/run?auth_token={token}"
    async with websockets.connect(url, max_size=200 * 1024 * 1024) as ws:
        if listen is not None:
            # RX only - no upload. SISO on `channel`, or MIMO when
            # --channels >= 2 (captures channels 0..N-1).
            handshake = {"mode": "listen", "n_samples": int(listen)}
            if channels and channels > 1:
                handshake["channels"] = int(channels)
            else:
                handshake["channel"] = int(channel)
            await ws.send(json.dumps(handshake))
            print(f"[listen] Requested {listen} samples "
                  + (f"on channels 0..{channels-1}" if channels > 1
                     else f"on channel {channel}"))
        else:
            with open(input_path, "rb") as f:
                data = f.read()
            is_mimo = data[:8] == b"MIMO\x00\x00\x00\x00"
            if channel and is_mimo:
                print("[error] --channel only applies to SISO (1-D) files; "
                      "in MIMO files column i always drives channel i")
                sys.exit(1)
            if channel:
                await ws.send(json.dumps({"mode": "siso",
                                          "channel": int(channel)}))
            await ws.send(data)
            print(f"[upload] Sent {input_path} ({len(data)} bytes)"
                  + (f" on channel {channel}" if channel else ""))

        while True:
            msg = await ws.recv()
            if isinstance(msg, bytes):
                with open(output_path, "wb") as f:
                    f.write(msg)
                print(f"[result] Saved to {output_path} ({len(msg)} bytes)")
                break
            else:
                info = json.loads(msg)
                if "error" in info:
                    print(f"[error] {info['error']}: {info.get('message', '')}")
                    sys.exit(1)
                elif info.get("message") == "info":
                    fc = info.get("carrier_frequency_hz", 0) / 1e6
                    bw = info.get("bandwidth_hz", 0) / 1e6
                    sr = info.get("sample_rate_hz", 0) / 1e6
                    print(f"[info] Carrier: {fc:.0f} MHz | BW: {bw:.0f} MHz | Rate: {sr:.0f} MSps")
                elif info.get("message") == "ack":
                    pass    # handshake accepted
                elif info.get("message") == "done":
                    print("[done] Processing complete, receiving file...")
                elif info.get("message") == "queued":
                    pos = info.get("queue_position", "?")
                    print(f"[queued] Task {info.get('uid', '')} - {pos} task(s) ahead in queue")
                elif info.get("message") == "status":
                    state = info.get("state", "?")
                    pos = info.get("queue_position", 0)
                    if state == "PD":
                        print(f"[waiting] Queue position: {pos} task(s) ahead")
                    elif state == "R":
                        print("[running] Processing your signal...")
                    elif state == "D":
                        print("[done] Task finished")
                else:
                    print(f"[server] {json.dumps(info)}")


def main():
    p = argparse.ArgumentParser(description="USRP Sandbox Client")
    p.add_argument("-i", "--input", help="Input .f32 file (omit when using --listen)")
    p.add_argument("-o", "--output", default="output.f32", help="Output .f32 file")
    p.add_argument("-s", "--server", default="localhost:8000", help="Server address")
    p.add_argument("-t", "--token", default="default-bench-token-2024", help="Auth token")
    p.add_argument("-c", "--channel", type=int, default=0,
                   help="Hardware channel for SISO send/listen (default 0)")
    p.add_argument("-l", "--listen", type=int, metavar="N_SAMPLES",
                   help="Receive only: capture N samples without transmitting")
    p.add_argument("--channels", type=int, default=1,
                   help="With --listen: capture this many channels at once (MIMO)")
    args = p.parse_args()
    if args.listen is None and not args.input:
        p.error("either -i/--input or -l/--listen is required")
    if args.listen is not None and args.input:
        p.error("-i/--input and -l/--listen are mutually exclusive")
    if args.channels > 1 and args.channel:
        p.error("--channel only applies to single-channel mode; "
                "--channels captures channels 0..N-1")
    asyncio.run(run(args.server, args.token, args.input, args.output,
                    channel=args.channel, listen=args.listen,
                    channels=args.channels))


if __name__ == "__main__":
    main()
