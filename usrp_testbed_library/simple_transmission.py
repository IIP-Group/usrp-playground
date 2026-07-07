
import argparse
try:
    from .usrp_common import not_negative_int, not_negative_float, positive_float, positive_int, valid_ip, valid_path, valid_port
except ImportError:
    from usrp_common import not_negative_int, not_negative_float, positive_float, positive_int, valid_ip, valid_path, valid_port
import zmq
import logging
import time
import threading
import os
import numpy as np

DEFAULT_TX_IP = "127.0.0.1"
DEFAULT_RX_IP = "127.0.0.1"

DEFAULT_TX_REP_PORT = "5557"
DEFAULT_TX_PUB_PORT = "5558"

DEFAULT_RX_REP_PORT = "5555"
DEFAULT_RX_PUB_PORT = "5556"

# Timeout Configuration (all in milliseconds except where noted)
CONNECTIVITY_TIMEOUT_MS = 1000      # Timeout for daemon connectivity test
CONFIGURATION_TIMEOUT_MS = 5000     # Timeout for USRP configuration (includes RFNoC delays)
SIGNAL_LOADING_TIMEOUT_MS = 10000   # Timeout for signal file loading
MSG_TIMEOUT_MS = 1000               # Default timeout for other messages

# Timing Configuration
INITIAL_DELAY = 1.0                 # Initial 1 second delay before starting (seconds)
OPERATION_TIMEOUT_MARGIN = 2.0      # Extra seconds for operation timeout (seconds)

def parse_cmd_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Records complex baseband samples from a USRP device.")

    parser.add_argument('--tx-sync-channel', '-sc', type=not_negative_int, nargs="+", required=True, help="USRP channel(s) to broadcast synchronization sequence from.")
    parser.add_argument('--tx-intf-channel', '-ic', type=not_negative_int, nargs="+", help="USRP channel(s) to broadcast interferering signal from. Optional - only needed if interference signal provided.")
    parser.add_argument('--rx-channels', '-rc', type=not_negative_int, nargs="+", required=True, help="USRP channels to use for reception.")
    parser.add_argument('--tx-rx-delay-diff', '-trd', type=positive_float, default=0.1, help="Additional delay for TX relative to RX start time in seconds.")
    parser.add_argument('--sync-signal-file', '-ssf', type=valid_path, required=True, help="Path to H5 file containing synchronization signal (tx_signal dataset).")
    parser.add_argument('--intf-signal-file', '-isf', type=valid_path, help="Path to H5 file containing interference signal (tx_signal dataset). Optional.")
    parser.add_argument('--tx-intf-gains', '-ig', type=not_negative_float, nargs="+", help="Gains for each of the interference TX channels in dB.")
    parser.add_argument('--tx-sync-gains', '-sg', type=not_negative_float, nargs="+", required=True, help="Gains for each of the synchronization TX channels in dB.")
    parser.add_argument('--rx-gain', '-rg', type=not_negative_float, default=30.0, help="Gain at the receiver.")
    parser.add_argument('--sampling-rate', '-fs', type=positive_float, help="Sampling rate in samples per second (used for both TX and RX if separate rates not specified).")
    parser.add_argument('--tx-sampling-rate', '-tx-fs', type=positive_float, help="TX sampling rate in samples per second (overrides --sampling-rate for TX).")
    parser.add_argument('--rx-sampling-rate', '-rx-fs', type=positive_float, help="RX sampling rate in samples per second (overrides --sampling-rate for RX).")
    parser.add_argument('--carrier-frequency', '-fc', type=positive_float, required=True, help="Carrier frequency for reception.")
    parser.add_argument('--rx-usrp-address', '-ip-usrp-rx', type=valid_ip, default="192.168.10.2", help="USRP device IP address at the RX side as a string.")
    parser.add_argument('--tx-usrp-address', '-ip-usrp-tx', type=valid_ip, default="192.168.10.2", help="USRP device IP address at the TX side as a string.")
    parser.add_argument('--master-clock-rate', '-mcr', type=positive_float, default=250e6, help="Master clock rate in Hz -- only 245.76MHz or 250MHz available for 200MHz bandwidth images.")
    parser.add_argument('--output-file', '-o', type=valid_path, required=True, help="Output file to save the recorded samples (.h5 format).") 
    parser.add_argument('--rx-address', '-ip-rx', type=valid_ip, default=DEFAULT_RX_IP, help="IP address of the machine running the RX daemon.")
    parser.add_argument('--rx-rep-port', '-rrp', type=valid_port, default=DEFAULT_RX_REP_PORT, help="Port number of the RX daemon REP socket.")
    parser.add_argument('--rx-pub-port', '-rpp', type=valid_port, default=DEFAULT_RX_PUB_PORT, help="Port number of the RX daemon PUB socket.")
    parser.add_argument('--tx-address', '-ip-tx', type=valid_ip, default=DEFAULT_TX_IP, help="IP address of the machine running the TX daemon.")
    parser.add_argument('--tx-rep-port', '-trp', type=valid_port, default=DEFAULT_TX_REP_PORT, help="Port number of the TX daemon REP socket.")
    parser.add_argument('--tx-pub-port', '-tpp', type=valid_port, default=DEFAULT_TX_PUB_PORT, help="Port number of the TX daemon PUB socket.")

    return parser.parse_args()


def log_setup_response(device_name, response, logger):
    """Log setup response with proper error, mismatch, and success formatting."""
    status = response.get("status")

    if status == "OK":
        logger.info(f"{device_name} configured successfully.")
    elif status == "MISMATCH":
        logger.warning(f"{device_name} configuration mismatch detected!")
        mismatches = response.get("mismatches", {})
        for channel, differences in mismatches.items():
            logger.warning(f"Channel {channel} mismatches:")
            for param, (actual, requested) in differences.items():
                if param in ["fs", "fc"]:
                    logger.warning(f"  {param}: actual={actual:.2e}, requested={requested:.2e}")
                else:
                    logger.warning(f"  {param}: actual={actual}, requested={requested}")
    elif status == "ERROR":
        error_msg = response.get("error", "Unknown error")
        logger.error(f"{device_name} configuration failed: {error_msg}")
    else:
        logger.error(f"{device_name} returned unexpected status: {status}")


def setup_zmq_socket(req_addr, sub_addr):
    """Setup a ZMQ REQ and SUB socket."""
    context = zmq.Context()

    req_socket = context.socket(zmq.REQ)
    req_socket.connect(req_addr)
    req_socket.setsockopt(zmq.RCVTIMEO, CONNECTIVITY_TIMEOUT_MS)  # Set timeout for connectivity test

    sub_socket = context.socket(zmq.SUB)
    sub_socket.connect(sub_addr)
    sub_socket.setsockopt(zmq.SUBSCRIBE, b"")  # Subscribe to all messages
    sub_socket.setsockopt(zmq.RCVTIMEO, MSG_TIMEOUT_MS)  # Set timeout in ms

    return context, req_socket, sub_socket


def cleanup_zmq_resources(context, req_socket, sub_socket, logger, label=""):
    """Cleanup ZMQ resources safely."""
    try:
        if label:
            logger.info(f"Cleaning up {label} ZMQ resources...")
        else:
            logger.info("Cleaning up ZMQ resources...")
        if req_socket:
            req_socket.setsockopt(zmq.LINGER, 0)  # Don't wait for pending messages
            req_socket.close()
        if sub_socket:
            sub_socket.setsockopt(zmq.LINGER, 0)  # Don't wait for pending messages
            sub_socket.close()
        if context:
            context.term()
        if label:
            logger.info(f"{label} ZMQ cleanup completed")
        else:
            logger.info("ZMQ cleanup completed")
    except Exception as e:
        logger.warning(f"Error during {label + ' ' if label else ''}ZMQ cleanup: {e}")


def calculate_signal_duration(total_samples, sampling_rate):
    """Calculate signal duration in seconds from sample count."""
    return total_samples / sampling_rate


def monitor_tx_events(sub_socket, logger, stop_event):
    """Monitor TX daemon asynchronous events."""
    events_received = []

    while not stop_event.is_set():
        try:
            message = sub_socket.recv_json(zmq.NOBLOCK)
            events_received.append(message)

            event_type = message.get("event", "unknown")
            if event_type in ["underflow", "seq_error", "time_error"]:
                daemon_id = message.get('daemon_id', 'unknown-daemon')
                channel = message.get('ch', 'unknown')
                timestamp = message.get('ts', 'unknown')
                logger.error(f"TX Event: {event_type} on channel {channel} from {daemon_id} at {timestamp}")
            elif event_type == "daemon_error":
                daemon_id = message.get('daemon_id', 'unknown-daemon')
                error_msg = message.get('error', 'unknown')
                timestamp = message.get('ts', 'unknown')
                logger.error(f"TX Daemon Error from {daemon_id}: {error_msg} at {timestamp}")
            elif event_type == "burst_ack":
                daemon_id = message.get('daemon_id', 'unknown-daemon')
                channel = message.get('ch', 'unknown')
                logger.info(f"TX Burst acknowledged on channel {channel} from {daemon_id}")

        except zmq.Again:
            # No message available, continue
            time.sleep(0.01)
        except Exception as e:
            logger.error(f"Error monitoring TX events: {e}")
            break

    return events_received


def main():

    args = parse_cmd_arguments()

    # Validate gain/channel count consistency
    if len(args.tx_sync_gains) != len(args.tx_sync_channel):
        raise ValueError("Number of TX sync gains must match number of TX sync channels.")
    if args.tx_intf_gains and args.tx_intf_channel and len(args.tx_intf_gains) != len(args.tx_intf_channel):
        raise ValueError("Number of TX intf gains must match number of TX intf channels.")

    # Validate and determine sampling rates
    if args.tx_sampling_rate is None and args.rx_sampling_rate is None:
        # Use common sampling rate for both
        if args.sampling_rate is None:
            raise ValueError("Must specify either --sampling-rate or both --tx-sampling-rate and --rx-sampling-rate")
        tx_sampling_rate = args.sampling_rate
        rx_sampling_rate = args.sampling_rate
    else:
        # Use individual sampling rates
        tx_sampling_rate = args.tx_sampling_rate if args.tx_sampling_rate is not None else args.sampling_rate
        rx_sampling_rate = args.rx_sampling_rate if args.rx_sampling_rate is not None else args.sampling_rate

        if tx_sampling_rate is None:
            raise ValueError("TX sampling rate not specified. Use --tx-sampling-rate or --sampling-rate")
        if rx_sampling_rate is None:
            raise ValueError("RX sampling rate not specified. Use --rx-sampling-rate or --sampling-rate")


    tx_daemon_rep_addr = f"tcp://{args.tx_address}:{args.tx_rep_port}"
    tx_daemon_pub_addr = f"tcp://{args.tx_address}:{args.tx_pub_port}"
    rx_daemon_rep_addr = f"tcp://{args.rx_address}:{args.rx_rep_port}"
    rx_daemon_pub_addr = f"tcp://{args.rx_address}:{args.rx_pub_port}"

    logging.basicConfig(
      level=logging.INFO,
      format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
      handlers=[
          logging.FileHandler('app.log'),
          logging.StreamHandler()  # Console output
      ]
    )

    logger = logging.getLogger(__name__)

    logger.info(f"Connecting to TX daemon at {tx_daemon_rep_addr}")
    tx_context, tx_req_socket, tx_sub_socket = setup_zmq_socket(
        tx_daemon_rep_addr, tx_daemon_pub_addr
    )
    logger.info("TX sockets created successfully")

    # Build gain dict mapping each channel to its gain
    G_TX = {ch: args.tx_sync_gains[idx] for idx, ch in enumerate(args.tx_sync_channel)}
    intf_channels = args.tx_intf_channel or []
    if intf_channels and args.tx_intf_gains:
        for idx, ch in enumerate(intf_channels):
            G_TX[ch] = args.tx_intf_gains[idx]

    # Setup transmitter
    tx_setup_command = {
        "op": "CONFIGURE_USRP",
        "fs": tx_sampling_rate,
        "fc": args.carrier_frequency,
        "sync_channels": args.tx_sync_channel,
        "intf_channels": intf_channels,
        "G_TX": G_TX,
        "antenna": "TX/RX0"
    }
    


    # Test connectivity with a simple PING
    logger.info("Testing TX daemon connectivity...")
    try:
        tx_req_socket.send_json({"op": "PING"})
        ping_response = tx_req_socket.recv_json()
        logger.info(f"TX daemon responded: {ping_response}")
    except Exception as e:
        logger.error(f"Failed to connect to TX daemon: {e}")
        cleanup_zmq_resources(tx_context, tx_req_socket, tx_sub_socket, logger, "TX")
        return

    tx_req_socket.send_json(tx_setup_command)
    tx_req_socket.setsockopt(zmq.RCVTIMEO, CONFIGURATION_TIMEOUT_MS)  # Timeout for configuration (includes potential channel reconfig delays)
    tx_setup_response = tx_req_socket.recv_json()  # Use REQ socket for response

    log_setup_response("Transmitter", tx_setup_response, logger)

    # Check if TX setup was successful
    if tx_setup_response.get("status") == "ERROR":
        logger.error("TX configuration failed. Aborting.")
        cleanup_zmq_resources(tx_context, tx_req_socket, tx_sub_socket, logger, "TX")
        return

    logger.info(f"Connecting to RX daemon at {rx_daemon_rep_addr}")
    rx_context, rx_req_socket, rx_sub_socket = setup_zmq_socket(
        rx_daemon_rep_addr, rx_daemon_pub_addr
    )
    logger.info("RX sockets created successfully")

    # Test RX connectivity
    logger.info("Testing RX daemon connectivity...")
    try:
        rx_req_socket.send_json({"op": "PING"})
        ping_response = rx_req_socket.recv_json()
        logger.info(f"RX daemon responded: {ping_response}")
    except Exception as e:
        logger.error(f"Failed to connect to RX daemon: {e}")
        cleanup_zmq_resources(tx_context, tx_req_socket, tx_sub_socket, logger, "TX")
        cleanup_zmq_resources(rx_context, rx_req_socket, rx_sub_socket, logger, "RX")
        return

    rx_setup_command = {
        "op": "CONFIGURE_USRP",
        "fs": rx_sampling_rate,
        "fc": args.carrier_frequency,
        "channels": args.rx_channels,
        "G_RX": args.rx_gain,
        "antenna": "RX1"
    }

    rx_req_socket.setsockopt(zmq.RCVTIMEO, CONFIGURATION_TIMEOUT_MS)
    rx_req_socket.send_json(rx_setup_command)
    rx_setup_response = rx_req_socket.recv_json()

    log_setup_response("Receiver", rx_setup_response, logger)

    # Check if RX setup was successful
    if rx_setup_response.get("status") == "ERROR":
        logger.error("RX configuration failed. Aborting.")
        cleanup_zmq_resources(tx_context, tx_req_socket, tx_sub_socket, logger, "TX")
        cleanup_zmq_resources(rx_context, rx_req_socket, rx_sub_socket, logger, "RX")
        return

    # Load TX signals
    logger.info("Loading TX signals...")
    tx_load_command = {
        "op": "LOAD_SIGNAL",
        "sync_signal_path": args.sync_signal_file
    }

    # Add interference signal if provided
    if args.intf_signal_file:
        tx_load_command["intf_signal_path"] = args.intf_signal_file

    tx_req_socket.send_json(tx_load_command)
    tx_req_socket.setsockopt(zmq.RCVTIMEO, SIGNAL_LOADING_TIMEOUT_MS)  # Timeout for signal loading
    tx_load_response = tx_req_socket.recv_json()

    if tx_load_response.get("status") != "OK":
        logger.error(f"TX signal loading failed: {tx_load_response.get('error', 'Unknown error')}")
        cleanup_zmq_resources(tx_context, tx_req_socket, tx_sub_socket, logger, "TX")
        cleanup_zmq_resources(rx_context, rx_req_socket, rx_sub_socket, logger, "RX")
        return

    # Extract signal information
    signal_info = tx_load_response.get("signal_info", {})
    total_samples = signal_info.get("total_samples", 0)
    loaded_sync_samples = signal_info.get("loaded_sync_samples", 0)
    loaded_intf_samples = signal_info.get("loaded_intf_samples", 0)

    logger.info("TX signals loaded successfully")
    logger.info(f"Sync signal: {loaded_sync_samples} samples")
    if loaded_intf_samples > 0:
        logger.info(f"Interference signal: {loaded_intf_samples} samples")
    logger.info(f"Total transmission: {total_samples} samples")

    # Check for configuration mismatches
    if loaded_intf_samples > 0 and not args.tx_intf_channel:
        logger.warning("Interference signal loaded but no interference channels configured - signal will be ignored")
    elif loaded_intf_samples == 0 and args.tx_intf_channel:
        logger.warning(f"Interference channels {args.tx_intf_channel} configured but no interference signal - channels will transmit zeros")

    # Calculate signal duration (based on TX sampling rate since that's what determines transmission time)
    signal_duration = calculate_signal_duration(total_samples, tx_sampling_rate)
    logger.info(f"Signal duration: {signal_duration:.6f} seconds (based on TX rate: {tx_sampling_rate:.0f} Hz)")

    # Calculate timing
    tx_start_delay = INITIAL_DELAY + args.tx_rx_delay_diff
    rx_start_delay = INITIAL_DELAY  # Start delay_diff before expected signal
    rx_duration = 2 * args.tx_rx_delay_diff + signal_duration  # Capture window: delay_diff before + signal + delay_diff after
    rx_samples_needed = int(np.round(rx_duration * rx_sampling_rate))

    logger.info(f"TX sampling rate: {tx_sampling_rate:.0f} Hz")
    logger.info(f"RX sampling rate: {rx_sampling_rate:.0f} Hz")

    logger.info(f"RX will start in {rx_start_delay:.3f}s and record {rx_samples_needed} samples ({rx_duration:.6f}s)")
    logger.info(f"TX scheduled to start in {tx_start_delay:.3f}s")

    # Start TX event monitoring
    tx_stop_event = threading.Event()
    tx_monitor_thread = threading.Thread(
        target=monitor_tx_events,
        args=(tx_sub_socket, logger, tx_stop_event),
        daemon=True
    )
    tx_monitor_thread.start()

    logger.info("Starting RX reception...")
    rx_receive_command = {
        "op": "RECEIVE_TO_FILE",
        "n_samples": rx_samples_needed,
        "path": args.output_file,
        "delay": rx_start_delay
    }

    rx_req_socket.send_json(rx_receive_command)

    # Send TX transmission command to daemon
    logger.info("Sending TX transmission command to daemon...")
    tx_transmit_command = {
        "op": "TRANSMIT_BURST",
        "delay": tx_start_delay
    }

    tx_req_socket.send_json(tx_transmit_command)

    # Wait for TX operation to complete
    tx_completion_time = tx_start_delay + signal_duration
    tx_timeout_ms = int(np.round((tx_completion_time + OPERATION_TIMEOUT_MARGIN) * 1000))
    logger.info(f"Waiting for TX completion (timeout: {tx_timeout_ms/1000:.2f}s)...")

    # Calculate RX timeout
    rx_completion_time = rx_start_delay + rx_duration
    rx_timeout_ms = int(np.round((rx_completion_time + OPERATION_TIMEOUT_MARGIN) * 1000))

    # Get TX response (operation errors are reported but don't abort)
    try:
        tx_req_socket.setsockopt(zmq.RCVTIMEO, tx_timeout_ms)
        tx_transmit_response = tx_req_socket.recv_json()
        tx_success = tx_transmit_response.get("status") == "OK"
        if tx_success:
            samples_transmitted = tx_transmit_response.get("samples_sent", 0)
            logger.info(f"TX transmission completed: {samples_transmitted} samples sent")
        else:
            samples_transmitted = 0
            logger.error(f"TX transmission failed: {tx_transmit_response.get('error', 'Unknown error')}")
    except zmq.Again:
        tx_success = False
        samples_transmitted = 0
        logger.error(f"TX transmission timed out after {tx_timeout_ms/1000:.2f}s - no response from daemon")

    try:
        rx_req_socket.setsockopt(zmq.RCVTIMEO, rx_timeout_ms)
        rx_receive_response = rx_req_socket.recv_json()
        rx_success = rx_receive_response.get("status") == "OK"
        if rx_success:
            samples_received = rx_receive_response.get("samples_received", 0)
            logger.info(f"RX reception completed: {samples_received} samples captured")
        else:
            samples_received = 0
            logger.error(f"RX reception failed: {rx_receive_response.get('error', 'Unknown error')}")
    except zmq.Again:
        rx_success = False
        samples_received = 0
        logger.error(f"RX reception timed out after {rx_timeout_ms/1000:.2f}s - no response from daemon")

    # Stop TX event monitoring
    tx_stop_event.set()
    tx_monitor_thread.join(timeout=5)

    # Get file information
    if os.path.exists(args.output_file):
        file_size = os.path.getsize(args.output_file)
        file_size_mb = file_size / (1024 * 1024)
    else:
        file_size = 0
        file_size_mb = 0

    # Print comprehensive test report
    logger.info("=" * 60)
    overall_success = tx_success and rx_success
    if overall_success:
        logger.info("TEST COMPLETED SUCCESSFULLY")
    else:
        if not tx_success:
            logger.error("TX FAILED")
        if not rx_success:
            logger.error("RX FAILED")
    logger.info("=" * 60)

    # TX details
    tx_channels = list(args.tx_sync_channel)
    if args.tx_intf_channel:
        tx_channels.extend(args.tx_intf_channel)

    logger.info(f"TX Channels: {len(tx_channels)} total")
    for idx, ch in enumerate(args.tx_sync_channel):
        logger.info(f"  - Sync channel {ch}: {args.tx_sync_gains[idx]} dB gain")

    if args.tx_intf_channel and args.tx_intf_gains:
        for idx, ch in enumerate(args.tx_intf_channel):
            silence_flag = " - SILENCE" if loaded_intf_samples == 0 else ""
            logger.info(f"  - Intf channel {ch}: {args.tx_intf_gains[idx]} dB gain{silence_flag}")

    samples_per_channel = total_samples  # Each channel transmits the full signal length

    # Detailed transmission summary
    total_samples_all_channels = samples_per_channel * len(tx_channels)
    logger.info(f"TX: {total_samples_all_channels} total samples across all channels ({samples_per_channel} per channel)")
    for ch in args.tx_sync_channel:
        logger.info(f"  - Sync channel {ch}: {samples_per_channel} samples")
    if args.tx_intf_channel:
        silence_flag = " - SILENCE" if loaded_intf_samples == 0 else ""
        for ch in args.tx_intf_channel:
            logger.info(f"  - Intf channel {ch}: {samples_per_channel} samples{silence_flag}")
    logger.info(f"TX Signal duration: {signal_duration:.6f} seconds")

    # RX details
    logger.info(f"RX Channels: {len(args.rx_channels)} total ({args.rx_channels})")
    logger.info(f"  - All channels: {args.rx_gain} dB gain")
    samples_per_rx_channel = np.round(samples_received / len(args.rx_channels)).astype(int) if args.rx_channels else 0
    logger.info(f"RX: {samples_received} total samples ({samples_per_rx_channel} per channel)")
    logger.info(f"RX Duration: {rx_duration:.6f} seconds")

    logger.info(f"Output file: {args.output_file}")
    logger.info(f"File size: {file_size_mb:.2f} MB ({file_size} bytes)")
    logger.info("=" * 60)

    # Simple cleanup at program end
    cleanup_zmq_resources(tx_context, tx_req_socket, tx_sub_socket, logger, "TX")
    cleanup_zmq_resources(rx_context, rx_req_socket, rx_sub_socket, logger, "RX")


if __name__ == "__main__":
    main()
    
    
    
    
    
    
    