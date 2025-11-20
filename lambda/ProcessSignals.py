"""
Lambda Function: ProcessSignals
Procesa señales ECG/IMU con filtrado wavelet cuando archivo llega a S3
Python 3.11 Runtime
"""

import json
import boto3
import os
import struct
import numpy as np
import pywt
from datetime import datetime
from io import BytesIO

# Clientes AWS
s3_client = boto3.client('s3')

# Configuración
OUTPUT_BUCKET = os.environ.get('OUTPUT_BUCKET', 'holter-processed-data')
REGION = os.environ.get('AWS_REGION', 'us-east-1')


class SignalProcessor:
    """Procesador de señales basado en holter_hybrid_system.py"""
    
    def __init__(self):
        self.ecg_sample_rate = 100
        self.imu_sample_rate = 100
    
    def adaptive_wavelet_filter(self, signal, wavelet='db4', level=5, threshold_scale=1.5):
        """
        Filtrado wavelet adaptativo para ECG
        Adaptado de holter_hybrid_system.py
        """
        coeffs = pywt.wavedec(signal, wavelet, level=level)
        
        sigma = np.median(np.abs(coeffs[-1])) / 0.6745
        threshold = threshold_scale * sigma * np.sqrt(2 * np.log(len(signal)))
        
        coeffs_filtered = [coeffs[0]]
        for i in range(1, len(coeffs)):
            coeffs_filtered.append(pywt.threshold(coeffs[i], threshold, mode='soft'))
        
        filtered_signal = pywt.waverec(coeffs_filtered, wavelet)
        
        if len(filtered_signal) > len(signal):
            filtered_signal = filtered_signal[:len(signal)]
        elif len(filtered_signal) < len(signal):
            filtered_signal = np.pad(filtered_signal, (0, len(signal) - len(filtered_signal)))
        
        return filtered_signal
    
    def detect_motion_segments(self, accel_data, window_size=100, threshold=0.5):
        """
        Detecta segmentos de movimiento usando acelerómetro
        """
        accel_magnitude = np.sqrt(np.sum(accel_data**2, axis=1))
        accel_smooth = np.convolve(accel_magnitude, np.ones(window_size)/window_size, mode='same')
        accel_detrended = accel_magnitude - accel_smooth
        motion_indicator = np.abs(accel_detrended) > threshold
        
        return motion_indicator
    
    def process_ecg_segment(self, ecg_data, motion_mask, wavelet_level=5):
        """
        Procesa segmento ECG con filtrado adaptativo
        """
        filtered = np.zeros_like(ecg_data)
        
        for i in range(3):  # 3 derivaciones
            if motion_mask.any():
                active_segments = ecg_data[motion_mask, i]
                if len(active_segments) > 100:
                    filtered[motion_mask, i] = self.adaptive_wavelet_filter(
                        active_segments, 
                        level=wavelet_level,
                        threshold_scale=2.0
                    )
            
            quiet_segments = ecg_data[~motion_mask, i]
            if len(quiet_segments) > 100:
                filtered[~motion_mask, i] = self.adaptive_wavelet_filter(
                    quiet_segments,
                    level=wavelet_level,
                    threshold_scale=1.0
                )
        
        return filtered


def parse_binary_file(file_data):
    """
    Parsea archivo binario del formato del dispositivo
    
    Estructura:
    - Header: 32 bytes (FileHeader struct)
    - ECG data: num_ecg_samples * 3 * 4 bytes (float32)
    - IMU data: num_imu_samples * 6 * 2 bytes (int16)
    """
    
    # Leer header (32 bytes)
    header_format = '<IHHIIHHI4s'  # little-endian
    header_size = struct.calcsize(header_format)
    header_data = struct.unpack(header_format, file_data[:header_size])
    
    header = {
        'magic': header_data[0],
        'version': header_data[1],
        'device_id': header_data[2],
        'session_id': header_data[3],
        'timestamp_start': header_data[4],
        'ecg_sample_rate': header_data[5],
        'imu_sample_rate': header_data[6],
        'num_ecg_samples': header_data[7]
    }
    
    # Validar magic number
    if header['magic'] != 0x44415441:
        raise ValueError(f"Invalid magic number: {hex(header['magic'])}")
    
    print(f"[PARSE] Header: {header}")
    
    # Calcular offsets
    ecg_size = header['num_ecg_samples'] * 3 * 4  # 3 derivaciones, 4 bytes/float
    imu_offset = header_size + ecg_size
    
    # Leer datos ECG
    ecg_data = np.frombuffer(
        file_data[header_size:imu_offset],
        dtype=np.float32
    ).reshape(-1, 3)
    
    # Leer datos IMU
    imu_data_raw = np.frombuffer(
        file_data[imu_offset:],
        dtype=np.int16
    ).reshape(-1, 6)
    
    # Convertir IMU a unidades físicas
    imu_data = imu_data_raw.astype(np.float32) / 2048.0 * 9.81  # m/s²
    
    return header, ecg_data, imu_data


def lambda_handler(event, context):
    """
    Handler principal - Se dispara cuando archivo .bin llega a S3
    
    Event esperado (S3 trigger):
    {
        "Records": [{
            "s3": {
                "bucket": {"name": "holter-raw-data"},
                "object": {"key": "raw/2025/11/16/esp32-holter-001/session_1234567890.bin"}
            }
        }]
    }
    """
    
    print(f"[INFO] Event recibido: {json.dumps(event)}")
    
    try:
        # Extraer info del evento S3
        record = event['Records'][0]
        bucket_name = record['s3']['bucket']['name']
        object_key = record['s3']['object']['key']
        
        print(f"[INFO] Procesando: s3://{bucket_name}/{object_key}")
        
        # Descargar archivo
        response = s3_client.get_object(Bucket=bucket_name, Key=object_key)
        file_data = response['Body'].read()
        
        file_size_mb = len(file_data) / (1024 * 1024)
        print(f"[INFO] Tamaño archivo: {file_size_mb:.2f} MB")
        
        # Parsear archivo binario
        header, ecg_data, imu_data = parse_binary_file(file_data)
        
        print(f"[INFO] ECG shape: {ecg_data.shape}, IMU shape: {imu_data.shape}")
        
        # Procesar señales
        processor = SignalProcessor()
        
        # Detectar movimiento
        accel_data = imu_data[:, :3]  # Primeros 3 canales son acelerómetro
        motion_mask = processor.detect_motion_segments(accel_data)
        
        motion_percentage = (motion_mask.sum() / len(motion_mask)) * 100
        print(f"[INFO] Movimiento detectado: {motion_percentage:.1f}%")
        
        # Filtrar ECG
        ecg_filtered = processor.process_ecg_segment(ecg_data, motion_mask)
        
        # Preparar datos procesados
        processed_data = {
            'header': header,
            'ecg_filtered': ecg_filtered.tolist(),
            'imu_data': imu_data.tolist(),
            'motion_mask': motion_mask.tolist(),
            'metadata': {
                'processing_timestamp': datetime.utcnow().isoformat(),
                'motion_percentage': float(motion_percentage),
                'ecg_samples': int(len(ecg_filtered)),
                'imu_samples': int(len(imu_data))
            }
        }
        
        # Guardar resultado en S3
        output_key = object_key.replace('raw/', 'processed/').replace('.bin', '.json')
        
        s3_client.put_object(
            Bucket=OUTPUT_BUCKET,
            Key=output_key,
            Body=json.dumps(processed_data),
            ContentType='application/json',
            Metadata={
                'original_file': object_key,
                'device_id': str(header['device_id']),
                'session_id': str(header['session_id']),
                'motion_percentage': f"{motion_percentage:.1f}"
            }
        )
        
        print(f"[SUCCESS] Procesado guardado en: s3://{OUTPUT_BUCKET}/{output_key}")
        
        # Actualizar DynamoDB (opcional)
        update_processing_status(header, output_key, motion_percentage)
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Procesamiento exitoso',
                'output_key': output_key,
                'motion_percentage': motion_percentage
            })
        }
        
    except Exception as e:
        print(f"[ERROR] {str(e)}")
        import traceback
        traceback.print_exc()
        
        return {
            'statusCode': 500,
            'body': json.dumps({
                'status': 'error',
                'message': str(e)
            })
        }


def update_processing_status(header, output_key, motion_percentage):
    """Actualiza estado en DynamoDB"""
    try:
        dynamodb = boto3.resource('dynamodb')
        table_name = os.environ.get('DYNAMODB_TABLE', 'holter-sessions')
        table = dynamodb.Table(table_name)
        
        table.update_item(
            Key={
                'device_id': str(header['device_id']),
                'session_id': str(header['session_id'])
            },
            UpdateExpression='SET #status = :status, processed_key = :key, motion_pct = :motion, processed_at = :ts',
            ExpressionAttributeNames={
                '#status': 'status'
            },
            ExpressionAttributeValues={
                ':status': 'processed',
                ':key': output_key,
                ':motion': motion_percentage,
                ':ts': datetime.utcnow().isoformat()
            }
        )
        
        print(f"[DynamoDB] Estado actualizado")
        
    except Exception as e:
        print(f"[WARNING] No se pudo actualizar DynamoDB: {e}")
w