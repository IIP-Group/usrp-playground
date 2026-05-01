import sys
import json
import asyncio
import argparse
import websockets


async def run(server, token, input_path, output_path):
    url = f"ws://{server}/ws/run?auth_token={token}"
    async with websockets.connect(url, max_size=200 * 1024 * 1024) as ws:
        with open(input_path, "rb") as f:
            data = f.read()
        await ws.send(data)
        print(f"[upload] Sent {input_path} ({len(data)} bytes)")

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
                elif info.get("message") == "done":
                    print("[done] Processing complete, receiving file...")
                elif info.get("message") == "queued":
                    pos = info.get("queue_position", "?")
                    print(f"[queued] Task {info.get('uid', '')} — {pos} task(s) ahead in queue")
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
    p = argparse.ArgumentParser(description="USRP Benchmark Client")
    p.add_argument("-i", "--input", required=True, help="Input .f32 file")
    p.add_argument("-o", "--output", default="output.f32", help="Output .f32 file")
    p.add_argument("-s", "--server", default="localhost:8000", help="Server address")
    p.add_argument("-t", "--token", default="default-bench-token-2024", help="Auth token")
    args = p.parse_args()
    asyncio.run(run(args.server, args.token, args.input, args.output))


if __name__ == "__main__":
    main()
