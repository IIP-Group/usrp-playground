%% Zadoff-Chu Signal Generator
% Generates a single Zadoff-Chu sequence using MATLAB's native zadoffChuSeq
% function and saves it as H5 file for TX daemon usage.

clear; clc;

%% ========================================================================
%% CUSTOMIZATION OPTIONS - Modify these parameters as needed
%% ========================================================================

% Zadoff-Chu sequence parameters
seq_length = 63;        % Length of base ZC sequence
zc_root = 29;           % Root index for ZC sequence

% Pulse-shaping filter parameters
P = 2;                     % Oversampling ratio
filter_span = 9;           % Transmit Filter Span (in symbols)
filter_rollof = 0.3;       % Filter roll-off factor
filter_zc_cutoff = 2;      % Filter zero-crossing cutoff

% Signal structure parameters
%repetitions = 20000;                    % Number of times to repeat the sequence
repetitions = 140e3;
%interval = ((seq_length + filter_span) * P - 1) * 2 ;         % Zero samples between repetitions
interval = 0;

% Amplitude scaling
max_amplitude = 0.2;         % Maximum amplitude for output signal (empty = no scaling)

% Output configuration
output_path = 'Research/sdr_toa_estimation/signals/intf_signal_extended.h5';    % Full path for output file

%% ========================================================================
%% SIGNAL GENERATION
%% ========================================================================

fprintf('=== Zadoff-Chu Signal Generator ===\n');
fprintf('Sequence length: %d\n', seq_length);
fprintf('ZC root: %d\n', zc_root);
fprintf('Repetitions: %d, Interval: %d samples\n', repetitions, interval);
fprintf('Output file: %s\n\n', output_path);

% Create output directory if needed
[output_dir, ~, ~] = fileparts(output_path);
if ~isempty(output_dir) && ~exist(output_dir, 'dir')
    mkdir(output_dir);
    fprintf('Created directory: %s\n', output_dir);
end

%% Generate Zadoff-Chu Signal
fprintf('Generating Zadoff-Chu signal...\n');

% Create pulse shaping filter 
tx_filter_ht = rcosdesign(filter_rollof, filter_span, P);
tx_filter_ht = reshape(tx_filter_ht, [], 1);
tx_filter_ht = tx_filter_ht(1:end-1, :);

% Generate base ZC sequence using MATLAB's native function
base_sequence = zadoffChuSeq(zc_root, seq_length);
base_sequence = reshape(base_sequence, [], 1);
upsampled_sequence = upsample(base_sequence, P);

%Convolve sequence with pulse-shaping filter
total_length = length(tx_filter_ht) + length(upsampled_sequence) - 1;
%Zero-pad both sequences to total_length
upsampled_sequence = [upsampled_sequence; zeros(total_length - length(upsampled_sequence), 1)];
tx_filter_ht = [tx_filter_ht; zeros(total_length - length(tx_filter_ht), 1)];

convolved_sequence = ifft(fft(upsampled_sequence, [], 1) .* fft(tx_filter_ht, [], 1), [], 1);

% Create signal block: convolved sequence + zero padding
if interval > 0
    signal_block = [convolved_sequence; zeros(interval, 1)];
else
    signal_block = convolved_sequence;
end

% Repeat the block - this is the transmission signal
tx_signal = repmat(signal_block, repetitions, 1);

% Convert to single precision complex
tx_signal = complex(single(real(tx_signal)), single(imag(tx_signal)));

% Scale tx_signal if specified
if ~isempty(max_amplitude)
    current_max = max(abs(tx_signal));
    if current_max > 0
        scaling_factor = max_amplitude / current_max;
        tx_signal = tx_signal * scaling_factor;
        fprintf('Scaled tx_signal: %.4f (%.4f -> %.4f)\n', scaling_factor, current_max, max_amplitude);
    else
        fprintf('Warning: TX signal has zero amplitude, no scaling applied\n');
    end
else
    fprintf('No scaling applied to tx_signal (max: %.4f)\n', max(abs(tx_signal)));
end

%% Save to H5 File
fprintf('Saving signal to H5 file...\n');

% Delete file if it exists (HDF5 requires deletion before overwrite)
if exist(output_path, 'file')
    delete(output_path);
end

% Save complex data as interleaved real/imaginary parts
% MATLAB h5create doesn't support ComplexType parameter
interleaved_size = [2, length(tx_signal)];
h5create(output_path, '/tx_signal', interleaved_size, 'Datatype', 'single');

% Interleave real and imaginary parts: [real_part; imag_part]
interleaved_data = [real(tx_signal).'; imag(tx_signal).'];
h5write(output_path, '/tx_signal', interleaved_data);

% Add attribute to indicate this is complex data
h5writeatt(output_path, '/tx_signal', 'complex_format', 'interleaved_real_imag');

% Save non-tiled convolved waveform for correlation
convolved_waveform_single = complex(single(real(convolved_sequence)), single(imag(convolved_sequence)));
conv_interleaved_size = [2, length(convolved_waveform_single)];
h5create(output_path, '/convolved_waveform', conv_interleaved_size, 'Datatype', 'single');

% Interleave convolved waveform: [real_part; imag_part]
conv_interleaved_data = [real(convolved_waveform_single).'; imag(convolved_waveform_single).'];
h5write(output_path, '/convolved_waveform', conv_interleaved_data);

% Add attribute to indicate this is complex data
h5writeatt(output_path, '/convolved_waveform', 'complex_format', 'interleaved_real_imag');
h5writeatt(output_path, '/convolved_waveform', 'description', 'Non-tiled convolved waveform for correlation');

% Save original Zadoff-Chu sequence as auxiliary data
original_zc_single = complex(single(real(base_sequence)), single(imag(base_sequence)));
zc_interleaved_size = [2, length(original_zc_single)];
h5create(output_path, '/original_zc_sequence', zc_interleaved_size, 'Datatype', 'single');

% Interleave original ZC sequence: [real_part; imag_part]
zc_interleaved_data = [real(original_zc_single).'; imag(original_zc_single).'];
h5write(output_path, '/original_zc_sequence', zc_interleaved_data);

% Add attribute to indicate this is complex data
h5writeatt(output_path, '/original_zc_sequence', 'complex_format', 'interleaved_real_imag');
h5writeatt(output_path, '/original_zc_sequence', 'description', 'Original Zadoff-Chu sequence for reference');

% Add essential metadata only
h5writeatt(output_path, '/tx_signal', 'description', 'Pulse-shaped Zadoff-Chu transmission signal');
h5writeatt(output_path, '/tx_signal', 'sequence_length', int32(seq_length));
h5writeatt(output_path, '/tx_signal', 'zc_root', int32(zc_root));
h5writeatt(output_path, '/tx_signal', 'repetitions', int32(repetitions));
h5writeatt(output_path, '/tx_signal', 'interval', int32(interval));
h5writeatt(output_path, '/tx_signal', 'total_samples', int32(length(tx_signal)));
h5writeatt(output_path, '/tx_signal', 'convolved_waveform_samples', int32(length(convolved_sequence)));
if ~isempty(max_amplitude)
    h5writeatt(output_path, '/tx_signal', 'max_amplitude', single(max_amplitude));
    h5writeatt(output_path, '/tx_signal', 'scaling_applied', 'true');
else
    h5writeatt(output_path, '/tx_signal', 'scaling_applied', 'false');
end

fprintf('✓ Saved: %s (%d samples)\n', output_path, length(tx_signal));
fprintf('✓ Saved convolved waveform: %d samples for correlation\n', length(convolved_waveform_single));
fprintf('✓ Saved original ZC sequence: %d samples for reference\n', length(original_zc_single));

%% Summary
fprintf('\n=== Generation Complete ===\n');

% Display signal properties
block_length = length(convolved_sequence) + interval;
fprintf('Signal structure:\n');
fprintf('  Base sequence: %d samples\n', seq_length);
fprintf('  Block length: %d samples (convolved + %d zero padding)\n', block_length, interval);
fprintf('  Total length: %d samples (%d blocks)\n', length(tx_signal), repetitions);

%% Verification Plot (optional)
plot_signal = false;  % Set to true to display generated signal

if plot_signal
    figure('Name', 'Generated Zadoff-Chu Signal');

    plot(real(tx_signal), 'b-', 'LineWidth', 1);
    hold on;
    plot(imag(tx_signal), 'r-', 'LineWidth', 1);
    title(sprintf('Zadoff-Chu TX Signal (Root=%d, Length=%d)', zc_root, seq_length));
    xlabel('Sample Index');
    ylabel('Amplitude');
    legend('Real', 'Imaginary', 'Location', 'best');
    grid on;
end

fprintf('\nScript execution completed successfully!\n');