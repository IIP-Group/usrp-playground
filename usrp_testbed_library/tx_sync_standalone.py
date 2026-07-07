import argparse
import numpy as np
import uhd
import os
import ipaddress
from uhd.types import TXMetadata, TimeSpec, TXAsyncMetadata, TXMetadataEventCode as E
import time
from commpy.sequences import zcsequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List
import logging

STREAM_TIMEOUT = 0.1  # Timeout for stream operations in seconds

def positive_int(value):
    """Custom argparse type for positive integers."""
    try:
        ivalue = int(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError(f"{value} is not a valid integer")
    if ivalue <= 0:
        raise argparse.ArgumentTypeError(f"{value} is not a positive integer")
    return ivalue


def not_negative_int(value):
    """Custom argparse type for non-negative integers."""
    try:
        ivalue = int(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError(f"{value} is not a valid integer")
    if ivalue < 0:
        raise argparse.ArgumentTypeError(f"{value} is not a non-negative integer")
    return ivalue


def positive_float(value):
    """Custom argparse type for positive floats."""
    try:
        fvalue = float(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError(f"{value} is not a valid float")
    if fvalue <= 0.0:
        raise argparse.ArgumentTypeError(f"{value} is not a positive float")
    return fvalue
    
def not_negative_float(value):
    """Custom argparse type for non-negative floats."""
    try:
        fvalue = float(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError(f"{value} is not a valid float")
    if fvalue < 0.0:
        raise argparse.ArgumentTypeError(f"{value} is not a non-negative float")
    return fvalue


def valid_path(path: str):
    try:
        os.path.exists(path)
        if path.endswith(".h5"):
            return path
        else:
            raise ValueError(f"File must have a .h5 extension: {path!r}")
    except (OSError, ValueError) as e:
        raise ValueError(f"Invalid path: {path!r}") from e
    
    
def valid_ip(s: str):
    try:
        ipaddress.ip_address(s)
        return s
    except ValueError as e:
        raise ValueError(f"Invalid IP address: {s!r}") from e


def parse_cmd_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Transmits complex baseband samples from a USRP device.")

    parser.add_argument('--sync-channel', '-sc', type=not_negative_int, required=True, help="USRP channel to broadcast synchronization sequence from.")
    parser.add_argument('--intf-channel', '-ic', type=not_negative_int, required=True, nargs="+", help="USRP channel(s) to broadcast interferering signal from.")
    parser.add_argument('--initial-delay', '-d', type=positive_float, default=1e-3, help="Initial delay in seconds before starting transmission.")
    parser.add_argument('--sequence-length', '-l', type=positive_int, default=63, help="Length of the Zadoff-Chu synchronization sequence.")
    parser.add_argument('--sync-root', '-sr', type=positive_int, default=25, help="Root index of the Zadoff-Chu synchronization sequence.")
    parser.add_argument('--intf-root', '-ir', type=positive_int, default=29, help="Root index of the Zadoff-Chu interferering sequence.")
    parser.add_argument('--intf-gain', '-ig', type=not_negative_float, default=0.0, help="Gain for the interferering signal in dB.")
    parser.add_argument('--sync-gain', '-sg', type=not_negative_float, default=30.0, help="Gain for the synchronization signal in dB.")
    parser.add_argument('--sampling-rate', '-r', type=positive_float, required=True, help="Sampling rate in samples per second.")
    parser.add_argument('--carrier-frequency', '-f', type=positive_float, required=True, help="Carrier frequency for reception.")
    parser.add_argument('--usrp-address', '-a', type=valid_ip, default="192.168.10.2", help="USRP device IP address as a string.")
    parser.add_argument('--master-clock-rate', '-m', type=positive_float, default=250e6, help="Master clock rate in Hz -- only 245.76MHz or 250MHz available for 200MHz bandwidth images.")
    parser.add_argument('--sequence-repetitions', '-n', type=positive_int, default=1, help="Number of times to repeat the synchronization sequence during transmission.")
    parser.add_argument('--transmission-interval', '-ti', type=not_negative_int, default=0, help="Interval in samples between consecutive transmissions of the synchronization sequence.")
    
    return parser.parse_args()




def setup_usrp(config: dict) -> uhd.usrp.MultiUSRP:
    
    usrp = uhd.usrp.MultiUSRP(f"addr={config['IP']},clock_source=internal,time_source=internal, master_clock_rate={config['MCR']}")

    config["channels"] = [config["sync_channel"]] + config["intf_channels"]

    n_channels = usrp.get_tx_num_channels()
    available_channels = list(range(n_channels))
    
    if any(ch not in available_channels for ch in config["channels"]):
        raise ValueError(f"Invalid channels {config['channels']}. Available channels: {available_channels}")
    
    logging.info("Available channels and corresponding daughter board/frontend index:")
    for ch in available_channels:
        ch_path = usrp.get_tx_subdev_spec().to_string().split()[ch]
        logging.info(f"Channel {ch}: {ch_path}")
    
    fs_range = usrp.get_tx_rates()
    available_fs = np.arange(fs_range.start(), fs_range.stop()+fs_range.step(), fs_range.step())
    
    if not np.any(np.isclose(config["fs"], available_fs)):
        raise ValueError(f"Invalid sampling rate {config['fs']}. Available rates: {available_fs}")
    
    fc_range = usrp.get_tx_freq_range()
    if not (fc_range.start() <= config["fc"] <= fc_range.stop()):
        raise ValueError(f"Invalid carrier frequency {config['fc']}. Valid range: {fc_range.start()} - {fc_range.stop()}")
    
    gain_range = usrp.get_tx_gain_range()
    available_gains = np.arange(gain_range.start(), gain_range.stop()+gain_range.step(), gain_range.step())
    if not np.any(np.isclose(config["G_TX_sync"], available_gains)):
        raise ValueError(f"Invalid synchronization gain {config['G_TX_sync']} dB. Available gains: {available_gains}")
    if not np.any(np.isclose(config["G_TX_intf"], available_gains)):
        raise ValueError(f"Invalid interference gain {config['G_TX_intf']} dB. Available gains: {available_gains}")
    
    
    for ch in config["channels"]:
        ch_type = "sync" if ch == config["sync_channel"] else "intf"
        
        # Set sampling rate
        usrp.set_tx_rate(config["fs"], ch)
        actual_rate = usrp.get_tx_rate(ch)
        logging.info(f"Channel {ch} ({'Synchronization' if ch_type == 'sync' else 'Interference'}) - Requested rate: {config['fs']/1e6:.2f} MHz | Actual rate: {actual_rate/1e6:.2f} MHz")
        
        # Set carrier frequency
        tune_req = uhd.types.TuneRequest(config["fc"])
        usrp.set_tx_freq(tune_req, ch)
        actual_freq = usrp.get_tx_freq(ch)
        logging.info(f"Channel {ch} ({'Synchronization' if ch_type == 'sync' else 'Interference'}) - Requested frequency: {config['fc']/1e9:.3f} GHz | Actual frequency: {actual_freq/1e9:.3f} GHz")
        
        # Set gain
        usrp.set_tx_gain(config[f"G_TX_{ch_type}"], ch)
        actual_gain = usrp.get_tx_gain(ch)
        logging.info(f"Channel {ch} ({'Synchronization' if ch_type == 'sync' else 'Interference'}) - Requested gain: {config[f'G_TX_{ch_type}']} dB | Actual gain: {actual_gain} dB")
        
        # Set antenna
        usrp.set_tx_antenna("TX/RX0", ch)  
        logging.info(f"Channel {ch} ({'Synchronization' if ch_type == 'sync' else 'Interference'}) - Antenna set to: {usrp.get_tx_antenna(ch)}")

    
    usrp.set_time_now(TimeSpec(0.0))
    
    return usrp


def transmit_finite_burst(usrp, tx_streamer, tx_signal: np.ndarray, channels: List[int], start_delay: float, timeout: float):
    
    chunk_size = tx_streamer.get_max_num_samps()
    _, n_requested_samples = tx_signal.shape
    samples_transmitted = 0
    
    # Schedule transmission start time according to the initial delay
    first_chunk = True
    tx_start_time = TimeSpec(usrp.get_time_now().get_real_secs() + start_delay)
    
    while samples_transmitted < n_requested_samples:
        
        final_sample_idx = min(samples_transmitted + chunk_size, n_requested_samples)
        
        n_samples_chunk = final_sample_idx - samples_transmitted
        tx_buffer = np.zeros((len(channels), n_samples_chunk), dtype=np.complex64)
        for i, ch in enumerate(channels):
            tx_buffer[i, :] = tx_signal[ch, samples_transmitted:final_sample_idx]
        
        metadata = TXMetadata()
        metadata.start_of_burst = first_chunk
        metadata.end_of_burst = (final_sample_idx == n_requested_samples)
        metadata.has_time_spec = first_chunk
        if first_chunk:
            metadata.time_spec = tx_start_time
            first_chunk = False
        
        n_tx_iter = tx_streamer.send(tx_buffer, metadata, timeout)
        if n_tx_iter == 0:
            raise RuntimeError("TX stream timed out. No samples were sent.")
        
        samples_transmitted += n_tx_iter
        
    return samples_transmitted
        
        
def wait_for_tx_ack(tx_streamer, channels, timeout, poll_dt=0.05):
    """
    Wait for BURST_ACK per channel; raise error on UNDERFLOW / SEQ_ERROR / TIME_ERROR.
    """
    pending = set(channels)
    t0 = time.time()
    metadata = TXAsyncMetadata()

    while time.time() - t0 < timeout and pending:
        
        msg_received = tx_streamer.recv_async_msg(metadata, poll_dt)
        if not msg_received:
            continue

        # Handle events
        event = metadata.event_code
        ch_number = getattr(metadata, "channel", None)

        if event in (E.underflow, getattr(E, "underflow_in_packet", E.underflow)):
            raise RuntimeError("TX UNDERFLOW: host did not keep DAC fed (gap in output).")
        if event in (E.seq_error, getattr(E, "seq_error_in_burst", E.seq_error)):
            raise RuntimeError("TX SEQ_ERROR: packet loss/reorder between host and USRP.")
        if event == E.time_error:
            raise RuntimeError("TX TIME_ERROR: first packet arrived too late.")
        if event == E.burst_ack:
            if ch_number is None:
                pending.clear()
            else:
                pending.discard(ch_number)

    if pending:
        raise TimeoutError(f"Timed out waiting for BURST_ACK on channel(s): {sorted(pending)}")


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    args = parse_cmd_arguments()
    usrp_found = bool(uhd.find(f"addr={args.usrp_address}"))
    
    if not usrp_found:
        raise RuntimeError(f"USRP device not found at address {args.usrp_address}")

    usrp_config_dict = {
        "IP": args.usrp_address,
        "fs": args.sampling_rate,
        "fc": args.carrier_frequency,
        "G_TX_sync": args.sync_gain,
        "G_TX_intf": args.intf_gain,
        "MCR": args.master_clock_rate,
        "sync_channel": int(args.sync_channel),
        "intf_channels": [int(ch) for ch in args.intf_channel],
    }

    usrp = setup_usrp(usrp_config_dict)
    
    st_args = uhd.usrp.StreamArgs("fc32", "sc16")
    st_args.channels = usrp_config_dict["channels"]
    tx_streamer = usrp.get_tx_stream(st_args)
    
    sync_seq = np.array(zcsequence(args.sync_root, args.sequence_length), dtype=np.complex64).reshape(-1,)
    sync_seq_base_block = np.concatenate((sync_seq, np.zeros((args.transmission_interval,), dtype=np.complex64)))
    sync_seq_full = np.tile(sync_seq_base_block, args.sequence_repetitions) 
    
    intf_seq = np.array(zcsequence(args.intf_root, args.sequence_length), dtype=np.complex64).reshape(-1,)
    intf_seq_base_block = np.concatenate((intf_seq, np.zeros((args.transmission_interval,), dtype=np.complex64)))
    intf_seq_full = np.tile(intf_seq_base_block, args.sequence_repetitions)
    
    tx_signal = np.zeros((max(usrp_config_dict["channels"]) + 1, (args.sequence_length + args.transmission_interval) * args.sequence_repetitions), dtype=np.complex64)
    tx_signal[usrp_config_dict["sync_channel"], :] = sync_seq_full
    for ch in usrp_config_dict["intf_channels"]:
        tx_signal[ch, :] = intf_seq_full
    
    signal_duration = tx_signal.shape[1] / usrp_config_dict["fs"]
    
    with ThreadPoolExecutor(max_workers=2) as executor:
        
        futures = {
            executor.submit(
                transmit_finite_burst,
                usrp, tx_streamer, tx_signal,
                usrp_config_dict["channels"],
                args.initial_delay, STREAM_TIMEOUT
                ): "transmit_finite_burst",
            executor.submit(
                wait_for_tx_ack,
                tx_streamer,
                usrp_config_dict["channels"],
                timeout=signal_duration + args.initial_delay + 2.0,
                poll_dt=0.05
            ): "wait_for_tx_ack"
        }
        
        n_tx_samples = None
    
        for ft in as_completed(futures):
            ft_name = futures[ft]
            try:
                ft_result = ft.result()
                if ft_name == "transmit_finite_burst":
                    n_tx_samples = ft_result
                elif ft_name == "wait_for_tx_ack":
                    logging.info(f"Transmission completed: {n_tx_samples} samples transmitted.")
            except Exception as e:
                if ft_name == "wait_for_tx_ack":
                    logging.warning(f"{e}")



if __name__ == "__main__":
    main()


    
