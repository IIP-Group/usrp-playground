
import argparse
import numpy as np
import uhd
import os
import ipaddress
import h5py
import time
from uhd.types import StreamCMD, StreamMode, RXMetadata, RXMetadataErrorCode, TimeSpec
import logging


STREAM_TIMEOUT = 0.1  # 100ms


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
    if not path.endswith(".h5"):
        raise ValueError(f"File must have a .h5 extension: {path!r}")
    return path
    
    
def valid_ip(s: str):
    try:
        ipaddress.ip_address(s)
        return s
    except ValueError as e:
        raise ValueError(f"Invalid IP address: {s!r}") from e


def parse_cmd_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Records complex baseband samples from a USRP device.")

    parser.add_argument('--number-samples', '-n', type=positive_int, required=True, help="Number of samples to record per antenna.")
    parser.add_argument('--sampling-rate', '-r', type=positive_float, required=True, help="Sampling rate in samples per second.")
    parser.add_argument('--carrier-frequency', '-f', type=positive_float, required=True, help="Carrier frequency for reception.")
    parser.add_argument('--gain', '-g', type=not_negative_float, default=20.0, help="Gain for reception in dB.")
    parser.add_argument('--usrp-address', '-a', type=valid_ip, default="192.168.10.2", help="USRP device IP address as a string.")
    parser.add_argument('--master-clock-rate', '-m', type=positive_float, default=250e6, help="Master clock rate in Hz -- only 245.76MHz or 250MHz available for 200MHz bandwidth images.")
    parser.add_argument('--reception-delay', '-d', type=positive_float, default=1e-3, help="Delay in seconds before starting reception.")
    parser.add_argument('--output-file', '-o', type=valid_path, required=True, help="Output file to save the recorded samples (.h5 format).")
    parser.add_argument('--channels', '-c', type=not_negative_int, nargs="+", required=True,help="USRP channels to use for reception.")

    return parser.parse_args()
    
    
def setup_usrp(config: dict) -> uhd.usrp.MultiUSRP:
    
    usrp = uhd.usrp.MultiUSRP(f"addr={config['IP']},clock_source=internal,time_source=internal, master_clock_rate={config['MCR']}")

    n_channels = usrp.get_rx_num_channels()
    available_channels = list(range(n_channels))
    
    if any(ch not in available_channels for ch in config["channels"]):
        raise ValueError(f"Invalid channels {config['channels']}. Available channels: {available_channels}")
    
    logging.info("Available channels and corresponding daughter board/frontend index:")
    for ch in available_channels:
        ch_path = usrp.get_rx_subdev_spec().to_string().split()[ch]
        logging.info(f"Channel {ch}: {ch_path}")
    
    fs_range = usrp.get_rx_rates()
    available_fs = np.arange(fs_range.start(), fs_range.stop()+fs_range.step(), fs_range.step())
    
    if not np.any(np.isclose(config["fs"], available_fs)):
        raise ValueError(f"Invalid sampling rate {config['fs']}. Available rates: {available_fs}")
    
    fc_range = usrp.get_rx_freq_range()
    if not (fc_range.start() <= config["fc"] <= fc_range.stop()):
        raise ValueError(f"Invalid carrier frequency {config['fc']}. Valid range: {fc_range.start()} - {fc_range.stop()}")
    
    gain_range = usrp.get_rx_gain_range()
    available_gains = np.arange(gain_range.start(), gain_range.stop()+gain_range.step(), gain_range.step())
    if not np.any(np.isclose(config["G_RX"], available_gains)):
        raise ValueError(f"Invalid gain {config['G_RX']}. Available gains: {available_gains}")
    
    
    for ch in config["channels"]:
        # Set sampling rate
        usrp.set_rx_rate(config["fs"], ch)
        actual_rate = usrp.get_rx_rate(ch)
        logging.info(f"Channel {ch} - Requested rate: {config['fs']/1e6:.2f} MHz | Actual rate: {actual_rate/1e6:.2f} MHz")
        
        # Set carrier frequency
        tune_req = uhd.types.TuneRequest(config["fc"])
        usrp.set_rx_freq(tune_req, ch)
        actual_freq = usrp.get_rx_freq(ch)
        logging.info(f"Channel {ch} - Requested frequency: {config['fc']/1e9:.3f} GHz | Actual frequency: {actual_freq/1e9:.3f} GHz")
        
        # Set gain
        usrp.set_rx_gain(config["G_RX"], ch)
        actual_gain = usrp.get_rx_gain(ch)
        logging.info(f"Channel {ch} - Requested gain: {config['G_RX']} dB | Actual gain: {actual_gain} dB")
        
        # Set antenna
        usrp.set_rx_antenna("RX1", ch)  # or "TX/RX" depending on your setup
        logging.info(f"Channel {ch} - Antenna set to: {usrp.get_rx_antenna(ch)}")

    usrp.set_time_now(TimeSpec(0.0))
    
    return usrp
    
    
def flush_rx_stream(n_channels, rx_streamer, timeout=STREAM_TIMEOUT):
    """
    Drain any stale samples from an RX streamer.
    """
    # Ensure continuous streaming is stopped
    rx_streamer.issue_stream_cmd(StreamCMD(StreamMode.stop_cont))
    
    time.sleep(STREAM_TIMEOUT)  # Wait a bit to ensure any ongoing streaming is stopped

    metadata = RXMetadata()
    chunk_size = rx_streamer.get_max_num_samps()
    rx_buffer = np.zeros((n_channels, chunk_size), dtype=np.complex64)

    while True:
        _ = rx_streamer.recv(rx_buffer, metadata, timeout)
        # Drain buffers completely. Exit on timeout only. Other errors might arise from previous stale runs and are ignored.
        if metadata.error_code == RXMetadataErrorCode.timeout:
            break
        


def receive_finite_samples(usrp, rx_streamer, start_delay, n_requested_samples, channels, timeout=STREAM_TIMEOUT):
    
    n_channels = len(channels)
    rx_signal = np.zeros((n_channels, n_requested_samples), dtype=np.complex64)
    
    rx_start_time = TimeSpec(usrp.get_time_now().get_real_secs() + start_delay)
    
    stream_cmd = StreamCMD(StreamMode.num_done)
    stream_cmd.num_samps = n_requested_samples
    stream_cmd.stream_now = False
    stream_cmd.time_spec = rx_start_time
    
    # Stream command to start at specified time -- synchronized among channels -- and for a finite number of samples
    rx_streamer.issue_stream_cmd(stream_cmd)
    
    metadata = RXMetadata()
    chunk_size = rx_streamer.get_max_num_samps()
    rx_buffer = np.zeros((n_channels, chunk_size), dtype=np.complex64)

    samples_received = 0
    while samples_received < n_requested_samples:
        n_rx_iter = rx_streamer.recv(rx_buffer, metadata, timeout)

        if metadata.error_code != RXMetadataErrorCode.none:
            raise RuntimeError(f"ERROR: {metadata.strerror()}")

        if n_rx_iter == 0:
            continue

        # Compute number of samples to copy to avoid overflow
        n_to_copy = min(n_rx_iter, n_requested_samples - samples_received)

        for ch in range(n_channels):
            rx_signal[ch, samples_received:samples_received + n_to_copy] = rx_buffer[ch, :n_to_copy]
        
        samples_received += n_to_copy
        
    return rx_signal, samples_received

         


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
        "G_RX": args.gain,
        "MCR": args.master_clock_rate,
        "channels": [int(ch) for ch in args.channels],
    }

    usrp = setup_usrp(usrp_config_dict)
    
    st_args = uhd.usrp.StreamArgs("fc32", "sc16")
    st_args.channels = usrp_config_dict["channels"]
    rx_streamer = usrp.get_rx_stream(st_args)
    
    # Flush RX streamer to remove stale samples
    flush_rx_stream(len(usrp_config_dict["channels"]), rx_streamer)
    
    # Receive new samples
    rx_signal, n_samples_received = receive_finite_samples(usrp, rx_streamer, args.reception_delay, args.number_samples, usrp_config_dict["channels"])


    if n_samples_received != args.number_samples:
        raise RuntimeError(f"Received {n_samples_received} samples, expected {args.number_samples}.")
    else:
        logging.info(f"Received a total of {n_samples_received} samples on {len(usrp_config_dict['channels'])} channels successfully.")
        logging.info(f"Signal duration: {args.number_samples / args.sampling_rate * 1e3:.3f} ms.")

    with h5py.File(args.output_file, "w") as f:
        f.create_dataset(
            "rx_signal",
            data=rx_signal,
            compression=None,  
        )
        # Optional metadata
        f["rx_signal"].attrs.update({
            "fs": args.sampling_rate,     
            "fc": args.carrier_frequency,       
            "gain_dB": args.gain,        
            "otw": "sc16",
            "cpu": "fc32",
        })
        
    logging.info(f"Saved received samples to {args.output_file}")
    


if __name__ == "__main__":
    main()
    
    
    
    
    
    
    
    
