"""
Lambda Function: ProcessSignals - VERSION SOLO ACELEROMETRO
Procesa señales ECG + Acelerómetro
"""

import json
import boto3
import os
import struct
import numpy as np
import pywt
from datetime import datetime
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from io import BytesIO, StringIO
from scipy import signal
import csv

# Clientes AWS
s3_client = boto3.client('s3')

# Configuración
OUTPUT_BUCKET = os.environ.get('OUTPUT_BUCKET', 'holter-processed-data')
REGION = os.environ.get('AWS_REGION', 'us-east-1')

# Escalas del ESP32
ECG_SCALE_FACTOR = 6553.6
ACCEL_SCALE = 16.0 / 32768.0  # Solo acelerómetro

# FRECUENCIAS HARDCODED
ECG_SAMPLE_RATE_HZ = 250
IMU_SAMPLE_RATE_HZ = 50  # Ajustado a 50Hz para reducir I2C


class SignalProcessor:
    """Procesador de señales ECG/IMU"""
    
    def __init__(self, ecg_fs=250, imu_fs=100):
        self.ecg_sample_rate = ecg_fs
        self.imu_sample_rate = imu_fs
        print(f"[PROCESSOR] Inicializado con ECG={ecg_fs}Hz, IMU={imu_fs}Hz")
    
    def notch_filter_60hz(self, signal_data, fs):
        """Filtro Notch para eliminar interferencia de 60Hz"""
        f0 = 60.0
        Q = 30.0
        
        if f0 >= fs / 2:
            print(f"[WARNING] Frecuencia notch {f0}Hz >= Nyquist {fs/2}Hz, saltando filtro")
            return signal_data
        
        b, a = signal.iirnotch(f0, Q, fs)
        filtered = signal.filtfilt(b, a, signal_data)
        return filtered
    
    def highpass_filter(self, signal_data, cutoff, fs, order=4):
        """Filtro pasa-altos para eliminar drift de línea base"""
        nyquist = 0.5 * fs
        normalized_cutoff = cutoff / nyquist
        
        if normalized_cutoff <= 0 or normalized_cutoff >= 1:
            print(f"[WARNING] HPF cutoff inválido: {normalized_cutoff:.4f}, saltando")
            return signal_data
        
        b, a = signal.butter(order, normalized_cutoff, btype='high')
        filtered = signal.filtfilt(b, a, signal_data)
        return filtered
    
    def lowpass_filter(self, signal_data, cutoff, fs, order=4):
        """Filtro pasa-bajos para eliminar ruido de alta frecuencia"""
        nyquist = 0.5 * fs
        normalized_cutoff = cutoff / nyquist
        
        if normalized_cutoff <= 0 or normalized_cutoff >= 1:
            print(f"[WARNING] LPF cutoff inválido: {normalized_cutoff:.4f}, ajustando a 0.95")
            normalized_cutoff = 0.95
        
        b, a = signal.butter(order, normalized_cutoff, btype='low')
        filtered = signal.filtfilt(b, a, signal_data)
        return filtered
    
    def preprocess_ecg(self, ecg_signal):
        """
        Preprocesamiento completo de ECG:
        1. Filtro pasa-altos 0.5Hz (elimina drift)
        2. Filtro pasa-bajos 100Hz (elimina ruido HF)
        3. Filtro notch 60Hz (elimina ruido eléctrico)
        """
        fs = self.ecg_sample_rate
        nyquist = fs / 2
        
        print(f"[PREPROCESS] fs={fs}Hz, Nyquist={nyquist}Hz, señal length={len(ecg_signal)}")
        
        # Paso 1: HPF 0.5 Hz
        ecg_hpf = self.highpass_filter(ecg_signal, cutoff=0.5, fs=fs)
        
        # Paso 2: LPF
        lpf_cutoff = min(100, nyquist * 0.8)
        print(f"[PREPROCESS] LPF cutoff ajustado a {lpf_cutoff}Hz")
        ecg_lpf = self.lowpass_filter(ecg_hpf, cutoff=lpf_cutoff, fs=fs)
        
        # Paso 3: Notch 60Hz
        if fs > 120:
            ecg_filtered = self.notch_filter_60hz(ecg_lpf, fs)
        else:
            ecg_filtered = ecg_lpf
            print("[PREPROCESS] Saltando notch (fs muy bajo)")
        
        return ecg_filtered
    
    def adaptive_wavelet_filter(self, sig, wavelet='db4', level=5, threshold_scale=1.5):
        """Filtrado wavelet adaptativo para ECG"""
        if len(sig) < 2**level:
            level = max(1, int(np.log2(len(sig))) - 1)
        
        coeffs = pywt.wavedec(sig, wavelet, level=level)
        
        sigma = np.median(np.abs(coeffs[-1])) / 0.6745
        threshold = threshold_scale * sigma * np.sqrt(2 * np.log(len(sig)))
        
        coeffs_filtered = [coeffs[0]]
        for i in range(1, len(coeffs)):
            coeffs_filtered.append(pywt.threshold(coeffs[i], threshold, mode='soft'))
        
        filtered_signal = pywt.waverec(coeffs_filtered, wavelet)
        
        if len(filtered_signal) > len(sig):
            filtered_signal = filtered_signal[:len(sig)]
        elif len(filtered_signal) < len(sig):
            filtered_signal = np.pad(filtered_signal, (0, len(sig) - len(filtered_signal)))
        
        return filtered_signal
    
    def detect_motion_segments(self, accel_data, window_size=50, threshold=0.3):
        """
        Detecta segmentos de movimiento usando SOLO acelerómetro
        accel_data: (N, 3) array con [ax, ay, az]
        """
        # Manejar caso sin datos IMU
        if len(accel_data) == 0:
            print("[MOTION] No hay datos IMU - asumiendo sin movimiento")
            # Retornar array vacío que será manejado correctamente
            return np.array([], dtype=bool)
        
        accel_magnitude = np.sqrt(np.sum(accel_data**2, axis=1))
        
        if len(accel_magnitude) >= window_size:
            accel_smooth = np.convolve(accel_magnitude, np.ones(window_size)/window_size, mode='same')
        else:
            accel_smooth = accel_magnitude
        
        accel_detrended = np.abs(accel_magnitude - accel_smooth)
        motion_indicator = accel_detrended > threshold
        
        if len(motion_indicator) > 0:
            motion_pct = (motion_indicator.sum() / len(motion_indicator)) * 100
            print(f"[MOTION] {motion_pct:.1f}% del tiempo en movimiento")
        else:
            print("[MOTION] Sin datos de movimiento")
        
        return motion_indicator
    
    def resample_motion_mask(self, motion_mask, target_length):
        """Resamplea máscara de movimiento de IMU rate a ECG rate"""
        # Manejar caso sin datos IMU
        if len(motion_mask) == 0:
            print(f"[RESAMPLE] Sin datos IMU - creando máscara vacía de {target_length} muestras")
            return np.zeros(target_length, dtype=bool)
        
        original_length = len(motion_mask)
        if original_length == target_length:
            return motion_mask
        
        indices = np.linspace(0, original_length - 1, target_length).astype(int)
        return motion_mask[indices]
    
    def detect_heart_rate(self, ecg_signal, lead_idx=1):
        fs = self.ecg_sample_rate
        
        # Recortar 1 segundo al inicio y 1 al final
        margin = int(1 * fs)
        if len(ecg_signal) > 2 * margin:
            ecg_trimmed = ecg_signal[margin:-margin]
        else:
            ecg_trimmed = ecg_signal
        
        duration_sec = len(ecg_trimmed) / fs
        
        # Distancia mínima: 0.3s (200 BPM máx)
        min_distance = int(0.33 * fs)
        
        # Normalizar señal
        ecg_norm = ecg_trimmed - np.mean(ecg_trimmed)
        signal_std = np.std(ecg_norm)
        min_height = signal_std * 3
        
        # Detectar picos (probar normal e invertida)
        r_peaks_pos, _ = signal.find_peaks(ecg_norm, height=min_height, distance=min_distance)
        r_peaks_neg, _ = signal.find_peaks(-ecg_norm, height=min_height, distance=min_distance)
        
        if len(r_peaks_neg) > len(r_peaks_pos):
            r_peaks = r_peaks_neg
        else:
            r_peaks = r_peaks_pos
        
        # Calcular BPM usando intervalos R-R
        if len(r_peaks) >= 2:
            rr_intervals = np.diff(r_peaks) / fs
            rr_mean = np.mean(rr_intervals)
            bpm = 60 / rr_mean
        else:
            bpm = 0
        
        print(f"[HR] Lead {['I', 'II', 'III'][lead_idx]}: {len(r_peaks)} picos, BPM={bpm:.1f}")
        
        # Ajustar índices de picos al offset original
        r_peaks_original = r_peaks + margin
        
        return bpm, r_peaks_original
    
    def process_ecg_with_motion(self, ecg_data, motion_mask_imu, wavelet_level=4):
        """Procesa ECG con filtrado adaptativo según movimiento"""
        n_samples, n_leads = ecg_data.shape
        filtered = np.zeros_like(ecg_data)
        preprocessed = np.zeros_like(ecg_data)
        heart_rates = {}
        
        # Resamplear máscara de movimiento a tasa ECG
        motion_mask = self.resample_motion_mask(motion_mask_imu, n_samples)
        
        print(f"[ECG] Procesando {n_leads} derivaciones, {n_samples} muestras @ {self.ecg_sample_rate}Hz")
        
        # PASO 1: Preprocesamiento (HPF + LPF + Notch)
        for lead_idx in range(n_leads):
            lead_name = ['I', 'II', 'III'][lead_idx]
            signal_raw = ecg_data[:, lead_idx]
            signal_preprocessed = self.preprocess_ecg(signal_raw)
            preprocessed[:, lead_idx] = signal_preprocessed
            print(f"[ECG] Lead {lead_name}: Filtros aplicados")
        
        # PASO 2: Filtrado wavelet adaptativo
        for lead_idx in range(n_leads):
            lead_name = ['I', 'II', 'III'][lead_idx]
            sig = preprocessed[:, lead_idx]
            
            # Verificar si hay datos de movimiento
            if len(motion_mask) > 0:
                motion_indices = np.where(motion_mask)[0]
                quiet_indices = np.where(~motion_mask)[0]
            else:
                # Sin datos IMU: procesar todo como "quieto"
                motion_indices = np.array([], dtype=int)
                quiet_indices = np.arange(len(sig))
                print(f"[ECG] Lead {lead_name}: Sin datos IMU - procesando sin detección de movimiento")
            
            # Inicializar con señal preprocesada
            filtered[:, lead_idx] = sig
            
            if len(motion_indices) > 100:
                motion_signal = sig[motion_indices]
                filtered_motion = self.adaptive_wavelet_filter(motion_signal, level=wavelet_level, threshold_scale=2.0)
                filtered[motion_indices, lead_idx] = filtered_motion
            
            if len(quiet_indices) > 100:
                quiet_signal = sig[quiet_indices]
                filtered_quiet = self.adaptive_wavelet_filter(quiet_signal, level=wavelet_level, threshold_scale=1.0)
                filtered[quiet_indices, lead_idx] = filtered_quiet
            
            # Detectar BPM
            bpm, r_peaks = self.detect_heart_rate(filtered[:, lead_idx], lead_idx)
            heart_rates[lead_name] = {
                'bpm': float(bpm),
                'num_beats': len(r_peaks),
                'r_peaks': r_peaks.tolist()
            }
        
        return filtered, preprocessed, heart_rates, motion_mask


def parse_binary_file(file_data):
    """Parsea archivo binario del ESP32 - VERSION SOLO ACELEROMETRO"""
    print(f"[PARSE] Archivo de {len(file_data)} bytes")
    
    # Header: magic(4) + version(2) + device_id(2) + session_id(4) + timestamp(4) + 
    # ecg_rate(2) + imu_rate(2) + num_ecg(4) + num_imu(4) = 28 bytes
    header_format = '<IHHIIHHII'
    header_size = struct.calcsize(header_format)
    
    print(f"[PARSE] Tamaño header esperado: {header_size} bytes")
    
    if len(file_data) < header_size:
        raise ValueError(f"Archivo muy pequeño: {len(file_data)} bytes < {header_size} bytes")
    
    header_data = struct.unpack(header_format, file_data[:header_size])
    
    header = {
        'magic': header_data[0],
        'version': header_data[1],
        'device_id': header_data[2],
        'session_id': header_data[3],
        'timestamp_start': header_data[4],
        'ecg_sample_rate_raw': header_data[5],
        'imu_sample_rate_raw': header_data[6],
        'num_ecg_samples': header_data[7],
        'num_imu_samples': header_data[8]
    }
    
    header['ecg_sample_rate'] = ECG_SAMPLE_RATE_HZ
    header['imu_sample_rate'] = IMU_SAMPLE_RATE_HZ
    
    # Validar magic number con fallback
    expected_magic = 0x45434744  # "ECGD"
    if header['magic'] != expected_magic:
        print(f"[WARNING] Magic number inválido: 0x{header['magic']:08X} (esperado: 0x{expected_magic:08X})")
        print(f"[WARNING] Intentando parsear de todas formas...")
    else:
        print(f"[PARSE] Magic number válido: 0x{header['magic']:08X}")
    
    print(f"[PARSE] Version: {header['version']}")
    print(f"[PARSE] ECG samples: {header['num_ecg_samples']}")
    print(f"[PARSE] IMU samples: {header['num_imu_samples']}")
    
    # Calcular tamaños
    ecg_sample_size = 6  # 3 x int16 (I, II, III)
    imu_sample_size = 6  # 3 x int16 (ax, ay, az)
    
    ecg_size = header['num_ecg_samples'] * ecg_sample_size
    ecg_start = header_size
    ecg_end = ecg_start + ecg_size
    
    imu_start = ecg_end
    imu_size = header['num_imu_samples'] * imu_sample_size
    
    print(f"[PARSE] ECG: offset {ecg_start}-{ecg_end} ({ecg_size} bytes)")
    print(f"[PARSE] IMU: offset {imu_start}+ ({imu_size} bytes esperados)")
    
    # Leer ECG
    ecg_data_raw = np.frombuffer(file_data[ecg_start:ecg_end], dtype=np.int16).reshape(-1, 3)
    ecg_data = ecg_data_raw.astype(np.float32) / ECG_SCALE_FACTOR
    print(f"[PARSE] ECG: shape={ecg_data.shape}, rango=[{ecg_data.min():.3f}, {ecg_data.max():.3f}] mV")
    
    # Leer IMU - SOLO ACELEROMETRO (3 valores)
    if header['num_imu_samples'] > 0:
        imu_raw = np.frombuffer(file_data[imu_start:imu_start + imu_size], dtype=np.int16)
        n_imu = len(imu_raw) // 3  # 3 valores por muestra (ax, ay, az)
        imu_raw = imu_raw[:n_imu * 3].reshape(-1, 3)
        
        # Solo acelerómetro
        imu_data = imu_raw.astype(np.float32) * ACCEL_SCALE
        
        print(f"[PARSE] IMU (Accel): shape={imu_data.shape}")
    else:
        # Sin datos IMU
        imu_data = np.zeros((0, 3), dtype=np.float32)
        print(f"[PARSE] IMU: Sin datos (shape=(0, 3))")
    
    # Calcular duración
    duration_ecg = len(ecg_data) / ECG_SAMPLE_RATE_HZ
    duration_imu = len(imu_data) / IMU_SAMPLE_RATE_HZ if len(imu_data) > 0 else 0
    print(f"[PARSE] Duración ECG: {duration_ecg:.2f}s, IMU: {duration_imu:.2f}s")
    
    return header, ecg_data, imu_data


def generate_csv_data(ecg_raw, ecg_filtered, imu_data, motion_mask):
    """Genera CSV con datos - VERSION SOLO ACELEROMETRO"""
    output = StringIO()
    writer = csv.writer(output)
    
    # Header
    writer.writerow([
        'time_ecg_s', 'ecg_I_raw_mV', 'ecg_II_raw_mV', 'ecg_III_raw_mV',
        'ecg_I_filt_mV', 'ecg_II_filt_mV', 'ecg_III_filt_mV',
        'time_imu_s', 'accel_x_g', 'accel_y_g', 'accel_z_g', 'motion_detected'
    ])
    
    n_ecg = len(ecg_raw)
    n_imu = len(imu_data)
    max_rows = max(n_ecg, n_imu)
    
    for i in range(max_rows):
        row = []
        
        # ECG data
        if i < n_ecg:
            t_ecg = i / ECG_SAMPLE_RATE_HZ
            row.extend([
                f"{t_ecg:.4f}",
                f"{ecg_raw[i, 0]:.4f}", f"{ecg_raw[i, 1]:.4f}", f"{ecg_raw[i, 2]:.4f}",
                f"{ecg_filtered[i, 0]:.4f}", f"{ecg_filtered[i, 1]:.4f}", f"{ecg_filtered[i, 2]:.4f}"
            ])
        else:
            row.extend(['', '', '', '', '', '', ''])
        
        # IMU data 
        if i < n_imu:
            t_imu = i / IMU_SAMPLE_RATE_HZ
            motion = 1 if i < len(motion_mask) and motion_mask[i] else 0
            row.extend([
                f"{t_imu:.4f}",
                f"{imu_data[i, 0]:.4f}", f"{imu_data[i, 1]:.4f}", f"{imu_data[i, 2]:.4f}",
                str(motion)
            ])
        else:
            row.extend(['', '', '', '', ''])
        
        writer.writerow(row)
    
    return output.getvalue()


def generate_plots(ecg_filtered, ecg_raw, imu_accel, motion_mask, metadata, heart_rates):
    """Genera visualizaciones"""
    n_ecg = len(ecg_filtered)
    n_imu = len(imu_accel)
    
    time_ecg = np.arange(n_ecg) / ECG_SAMPLE_RATE_HZ
    time_imu = np.arange(n_imu) / IMU_SAMPLE_RATE_HZ if n_imu > 0 else np.array([])
    
    # Resamplear motion_mask para ECG
    if len(motion_mask) > 0 and len(motion_mask) != n_ecg:
        indices = np.linspace(0, len(motion_mask) - 1, n_ecg).astype(int)
        motion_mask_ecg = motion_mask[indices]
    else:
        motion_mask_ecg = motion_mask if len(motion_mask) > 0 else np.zeros(n_ecg, dtype=bool)
    
    plots = {}
    
    duration_sec = n_ecg / ECG_SAMPLE_RATE_HZ
    print(f"[PLOTS] Duración: {duration_sec:.2f}s, ECG: {n_ecg}, IMU: {n_imu}")
    
    # ========== PLOT 1: ECG Filtrado ==========
    fig, axes = plt.subplots(3, 1, figsize=(14, 10))
    fig.suptitle(f'ECG Filtrado - 3 Derivaciones ({duration_sec:.1f}s)', fontsize=14, fontweight='bold')
    
    lead_names = ['I', 'II', 'III']
    for i, ax in enumerate(axes):
        lead_name = lead_names[i]
        ax.plot(time_ecg, ecg_filtered[:, i], color='darkblue', linewidth=0.8)
        
        if lead_name in heart_rates and 'r_peaks' in heart_rates[lead_name]:
            r_peaks = np.array(heart_rates[lead_name]['r_peaks'])
            if len(r_peaks) > 0:
                ax.scatter(time_ecg[r_peaks], ecg_filtered[r_peaks, i], 
                          c='red', s=50, marker='x', linewidths=2, label='R peaks')
        
        bpm_text = f"{heart_rates[lead_name]['bpm']:.1f} BPM" if lead_name in heart_rates else "N/A"
        ax.set_ylabel(f'{lead_name} (mV)', fontsize=10)
        ax.set_title(f'Lead {lead_name} - {bpm_text}', fontsize=11)
        ax.grid(True, alpha=0.3)
        if i == 0:
            ax.legend(loc='upper right', fontsize=9)
    
    axes[-1].set_xlabel('Tiempo (s)', fontsize=11)
    plt.tight_layout()
    
    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=150)
    buf.seek(0)
    plots['ecg_filtered.png'] = buf.getvalue()
    plt.close()
    
    # ========== PLOT 2: Comparación Raw vs Filtrado ==========
    fig, axes = plt.subplots(2, 1, figsize=(14, 8), sharex=True)
    bpm_ii = heart_rates.get('II', {}).get('bpm', 0)
    fig.suptitle(f'Comparación: ECG Raw vs Filtrado (Lead II) - {bpm_ii:.1f} BPM', fontsize=14, fontweight='bold')
    
    axes[0].plot(time_ecg, ecg_raw[:, 1], color='gray', linewidth=0.5, alpha=0.7)
    axes[0].set_ylabel('Raw (mV)', fontsize=10)
    axes[0].grid(True, alpha=0.3)
    axes[0].set_title('Señal Original')
    
    axes[1].plot(time_ecg, ecg_filtered[:, 1], color='darkgreen', linewidth=0.8)
    if 'II' in heart_rates and 'r_peaks' in heart_rates['II']:
        r_peaks = np.array(heart_rates['II']['r_peaks'])
        if len(r_peaks) > 0:
            axes[1].scatter(time_ecg[r_peaks], ecg_filtered[r_peaks, 1], 
                          c='red', s=60, marker='x', linewidths=2, label='R peaks')
    axes[1].set_ylabel('Filtrado (mV)', fontsize=10)
    axes[1].set_xlabel('Tiempo (s)', fontsize=11)
    axes[1].grid(True, alpha=0.3)
    axes[1].set_title('Después de Filtrado')
    axes[1].legend(loc='upper right', fontsize=9)
    axes[1].set_xlim(0, time_ecg[-1])
    
    plt.tight_layout()
    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=150)
    buf.seek(0)
    plots['ecg_comparison.png'] = buf.getvalue()
    plt.close()
    
    # ========== PLOT 3: Dashboard (solo si hay datos IMU) ==========
    if n_imu > 0:
        fig = plt.figure(figsize=(16, 10))
        gs = fig.add_gridspec(4, 1, hspace=0.3)
        
        ax1 = fig.add_subplot(gs[0:2])
        ax1.plot(time_ecg, ecg_filtered[:, 1], color='darkblue', linewidth=0.8)
        if 'II' in heart_rates and 'r_peaks' in heart_rates['II']:
            r_peaks = np.array(heart_rates['II']['r_peaks'])
            if len(r_peaks) > 0:
                ax1.scatter(time_ecg[r_peaks], ecg_filtered[r_peaks, 1], 
                           c='red', s=50, marker='x', linewidths=2, label='R peaks')
        bpm_text = f" - {heart_rates.get('II', {}).get('bpm', 0):.1f} BPM"
        ax1.set_title(f'ECG Lead II{bpm_text}', fontsize=12, fontweight='bold')
        ax1.set_ylabel('Amplitud (mV)', fontsize=10)
        ax1.grid(True, alpha=0.3)
        ax1.legend(loc='upper right', fontsize=9)
        ax1.set_xlim(0, time_ecg[-1])
        
        ax2 = fig.add_subplot(gs[2])
        accel_mag = np.sqrt(np.sum(imu_accel**2, axis=1))
        ax2.plot(time_imu, accel_mag, color='red', linewidth=0.8)
        ax2.set_title('Aceleración Total', fontsize=11)
        ax2.set_ylabel('Magnitud (g)', fontsize=9)
        ax2.set_xlabel('Tiempo (s)', fontsize=9)
        ax2.grid(True, alpha=0.3)
        ax2.set_xlim(0, time_imu[-1])
        
        ax3 = fig.add_subplot(gs[3])
        ax3.fill_between(time_imu, 0, motion_mask.astype(float), color='orange', alpha=0.5)
        ax3.set_title('Máscara de Movimiento', fontsize=11)
        ax3.set_ylabel('Movimiento', fontsize=9)
        ax3.set_xlabel('Tiempo (s)', fontsize=9)
        ax3.set_yticks([0, 1])
        ax3.set_yticklabels(['Quieto', 'Movimiento'])
        ax3.grid(True, alpha=0.3)
        ax3.set_xlim(0, time_imu[-1])
        
        avg_bpm = np.mean([hr['bpm'] for hr in heart_rates.values()]) if heart_rates else 0
        fig.suptitle(f'Dashboard Holter - {duration_sec:.1f}s - {avg_bpm:.1f} BPM - Mov: {metadata["motion_percentage"]:.1f}%', 
                     fontsize=14, fontweight='bold', y=0.995)
    else:
        # Dashboard simplificado sin IMU
        fig, ax = plt.subplots(figsize=(14, 6))
        ax.plot(time_ecg, ecg_filtered[:, 1], color='darkblue', linewidth=0.8)
        if 'II' in heart_rates and 'r_peaks' in heart_rates['II']:
            r_peaks = np.array(heart_rates['II']['r_peaks'])
            if len(r_peaks) > 0:
                ax.scatter(time_ecg[r_peaks], ecg_filtered[r_peaks, 1], 
                          c='red', s=50, marker='x', linewidths=2, label='R peaks')
        avg_bpm = np.mean([hr['bpm'] for hr in heart_rates.values()]) if heart_rates else 0
        ax.set_title(f'ECG Lead II - {avg_bpm:.1f}', fontsize=14, fontweight='bold')
        ax.set_ylabel('Amplitud (mV)', fontsize=11)
        ax.set_xlabel('Tiempo (s)', fontsize=11)
        ax.grid(True, alpha=0.3)
        ax.legend(loc='upper right')
        ax.set_xlim(0, time_ecg[-1])
    
    plt.tight_layout()
    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=150)
    buf.seek(0)
    plots['dashboard.png'] = buf.getvalue()
    plt.close()
    
    # ========== PLOT 4: IMU Acelerómetro (solo si hay datos) ==========
    if n_imu > 0:
        fig, axes = plt.subplots(3, 1, figsize=(14, 8), sharex=True)
        fig.suptitle('Acelerómetro - 3 Ejes', fontsize=14, fontweight='bold')
        
        axis_names = ['X', 'Y', 'Z']
        colors = ['red', 'green', 'blue']
        
        for i, ax in enumerate(axes):
            ax.plot(time_imu, imu_accel[:, i], color=colors[i], linewidth=0.8)
            ax.set_ylabel(f'Accel {axis_names[i]} (g)', fontsize=10)
            ax.grid(True, alpha=0.3)
        
        axes[-1].set_xlabel('Tiempo (s)', fontsize=11)
        axes[-1].set_xlim(0, time_imu[-1])
        plt.tight_layout()
        
        buf = BytesIO()
        plt.savefig(buf, format='png', dpi=150)
        buf.seek(0)
        plots['imu_accel.png'] = buf.getvalue()
        plt.close()
    
    print(f"[PLOTS] Generadas {len(plots)} imágenes")
    return plots


def lambda_handler(event, context):
    """Handler principal"""
    print(f"[INFO] Event: {json.dumps(event, indent=2)}")
    
    try:
        record = event['Records'][0]
        bucket_name = record['s3']['bucket']['name']
        object_key = record['s3']['object']['key']
        
        print(f"[INFO] Procesando: s3://{bucket_name}/{object_key}")
        
        # Descargar
        response = s3_client.get_object(Bucket=bucket_name, Key=object_key)
        file_data = response['Body'].read()
        print(f"[INFO] Descargado: {len(file_data) / 1024:.2f} KB")
        
        # Parsear
        header, ecg_data, imu_data = parse_binary_file(file_data)
        
        # Crear procesador
        processor = SignalProcessor(ecg_fs=ECG_SAMPLE_RATE_HZ, imu_fs=IMU_SAMPLE_RATE_HZ)
        
        # Detectar movimiento (solo si hay datos IMU)
        if len(imu_data) > 0:
            accel_data = imu_data  # Ya es solo acelerómetro (3 columnas)
            motion_mask_imu = processor.detect_motion_segments(accel_data)
            motion_percentage = (motion_mask_imu.sum() / len(motion_mask_imu)) * 100 if len(motion_mask_imu) > 0 else 0
        else:
            motion_mask_imu = np.array([], dtype=bool)
            motion_percentage = 0
            print("[INFO] Sin datos IMU")
        
        # Procesar ECG
        print("[INFO] Procesando ECG...")
        ecg_filtered, ecg_preprocessed, heart_rates, motion_mask_ecg = processor.process_ecg_with_motion(
            ecg_data, motion_mask_imu
        )
        
        # BPM promedio
        avg_bpm = np.mean([hr['bpm'] for hr in heart_rates.values()]) if heart_rates else 0
        
        # Metadata
        duration_sec = len(ecg_data) / ECG_SAMPLE_RATE_HZ
        metadata = {
            'processing_timestamp': datetime.utcnow().isoformat(),
            'source_file': object_key,
            'duration_seconds': float(duration_sec),
            'motion_percentage': float(motion_percentage),
            'ecg_samples': int(len(ecg_filtered)),
            'imu_samples': int(len(imu_data)),
            'ecg_sample_rate_hz': ECG_SAMPLE_RATE_HZ,
            'imu_sample_rate_hz': IMU_SAMPLE_RATE_HZ,
            'header_ecg_rate': header['ecg_sample_rate_raw'],
            'header_imu_rate': header['imu_sample_rate_raw'],
            'imu_mode': 'accelerometer_only',
            'heart_rate': {
                'average_bpm': float(avg_bpm),
                'lead_I': heart_rates.get('I', {}),
                'lead_II': heart_rates.get('II', {}),
                'lead_III': heart_rates.get('III', {})
            }
        }
        
        print(f"\n[RESULTS] Duración: {duration_sec:.1f}s")
        print(f"[RESULTS] BPM Promedio: {avg_bpm:.1f}")
        for lead, hr in heart_rates.items():
            print(f"[RESULTS] Lead {lead}: {hr['bpm']:.1f} BPM ({hr['num_beats']} latidos)")
        
        # Generar plots
        print("[INFO] Generando visualizaciones...")
        plots = generate_plots(
            ecg_filtered, ecg_data, imu_data, 
            motion_mask_imu, metadata, heart_rates
        )
        
        # Generar CSV con datos
        print("[INFO] Generando CSV...")
        csv_data = generate_csv_data(ecg_data, ecg_filtered, imu_data, motion_mask_imu)
        
        # Base path
        base_key = object_key.replace('raw/', 'processed/').replace('.bin', '')
        
        uploaded_files = []
        
        # Subir imágenes
        for filename, image_data in plots.items():
            output_key = f"{base_key}_{filename}"
            s3_client.put_object(
                Bucket=OUTPUT_BUCKET, Key=output_key,
                Body=image_data, ContentType='image/png'
            )
            uploaded_files.append(output_key)
            print(f"[SUCCESS] {output_key}")
        
        # Subir CSV
        csv_key = f"{base_key}_signals.csv"
        s3_client.put_object(
            Bucket=OUTPUT_BUCKET, Key=csv_key,
            Body=csv_data.encode('utf-8'), ContentType='text/csv'
        )
        uploaded_files.append(csv_key)
        print(f"[SUCCESS] {csv_key}")
        
        # Subir metadata JSON
        metadata_key = f"{base_key}_metadata.json"
        s3_client.put_object(
            Bucket=OUTPUT_BUCKET, Key=metadata_key,
            Body=json.dumps(metadata, indent=2), ContentType='application/json'
        )
        uploaded_files.append(metadata_key)
        print(f"[SUCCESS] {metadata_key}")
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'OK',
                'uploaded_files': uploaded_files,
                'duration_seconds': duration_sec,
                'average_bpm': avg_bpm,
                'motion_pct': motion_percentage,
                'imu_mode': 'accelerometer_only'
            })
        }
        
    except Exception as e:
        print(f"[ERROR] {str(e)}")
        import traceback
        traceback.print_exc()
        return {'statusCode': 500, 'body': json.dumps({'error': str(e)})}