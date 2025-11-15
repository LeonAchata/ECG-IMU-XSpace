import serial
import serial.tools.list_ports
import numpy as np
import pandas as pd
import pywt
from datetime import datetime
import os
import time

# =============================================================================
# CONFIGURACIÓN GLOBAL
# =============================================================================

class HolterConfig:
    """Configuración centralizada del sistema Holter"""
    
    # Serial USB
    BAUD_RATE = 115200
    SERIAL_TIMEOUT = 2.0
    
    # Frecuencia de muestreo
    SAMPLE_RATE = 100  # Hz
    
    # Procesamiento Wavelet
    WAVELET_TYPE = 'db4'
    DECOMPOSITION_LEVEL = 5
    
    # Detección de movimiento
    ACC_THRESHOLD_PERCENTILE = 75
    THRESHOLD_MULTIPLIER_HIGH_MOTION = 2.5
    THRESHOLD_MULTIPLIER_LOW_MOTION = 1.0
    
    # Archivos de salida
    OUTPUT_FOLDER = r'C:\Users\Lenovo\OneDrive\Desktop\PUCP\Instru\Holter_Data'
    
    @staticmethod
    def create_session_folder():
        """Crea carpeta para la sesión actual"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_folder = os.path.join(HolterConfig.OUTPUT_FOLDER, f"Session_{timestamp}")
        os.makedirs(session_folder, exist_ok=True)
        return session_folder
    
    @staticmethod
    def find_xspace_port():
        """Encuentra automáticamente el puerto del XSpace"""
        ports = serial.tools.list_ports.comports()
        
        for port in ports:
            if any(keyword in port.description.upper() for keyword in 
                   ['USB', 'SERIAL', 'COM', 'CP210', 'CH340', 'UART']):
                print(f"[INFO] Puerto encontrado: {port.device} - {port.description}")
                return port.device
        
        if ports:
            print("\n[INFO] Puertos disponibles:")
            for i, port in enumerate(ports):
                print(f"  {i+1}. {port.device} - {port.description}")
            return None
        else:
            print("[ERROR] No se encontraron puertos seriales")
            return None


# =============================================================================
# FUNCIONES DE PROCESAMIENTO WAVELET
# =============================================================================

def calculate_acceleration_magnitude(acc_x, acc_y, acc_z):
    """Calcula magnitud vectorial de aceleración"""
    return np.sqrt(acc_x**2 + acc_y**2 + acc_z**2)


def detect_motion_segments(acc_magnitude, threshold):
    """Detecta segmentos con movimiento significativo"""
    return acc_magnitude > threshold


def apply_wavelet_thresholding(coeffs, threshold_value, mode='soft'):
    """Aplica umbral a coeficientes wavelet"""
    return pywt.threshold(coeffs, threshold_value, mode=mode)


def adaptive_wavelet_filter(ecg_signal, motion_mask, wavelet='db4', level=5):
    """Aplica filtrado wavelet adaptativo basado en detección de movimiento"""
    
    # Descomposición wavelet
    coeffs = pywt.wavedec(ecg_signal, wavelet, level=level)
    
    # Calcular umbral base usando MAD
    sigma = np.median(np.abs(coeffs[-1])) / 0.6745
    threshold_base = sigma * np.sqrt(2 * np.log(len(ecg_signal)))
    
    # Procesar coeficientes
    coeffs_filtered = [coeffs[0]]  # Mantener aproximación
    
    for i in range(1, len(coeffs)):
        detail_coeffs = coeffs[i]
        level_factor = 1.5 ** (len(coeffs) - i)
        
        # Umbral adaptativo según movimiento
        if np.mean(motion_mask) > 0.3:
            threshold = threshold_base * HolterConfig.THRESHOLD_MULTIPLIER_HIGH_MOTION * level_factor
        else:
            threshold = threshold_base * HolterConfig.THRESHOLD_MULTIPLIER_LOW_MOTION * level_factor
        
        detail_filtered = apply_wavelet_thresholding(detail_coeffs, threshold, mode='soft')
        coeffs_filtered.append(detail_filtered)
    
    # Reconstrucción
    ecg_filtered = pywt.waverec(coeffs_filtered, wavelet)
    
    # Ajustar longitud
    if len(ecg_filtered) > len(ecg_signal):
        ecg_filtered = ecg_filtered[:len(ecg_signal)]
    elif len(ecg_filtered) < len(ecg_signal):
        ecg_filtered = np.pad(ecg_filtered, (0, len(ecg_signal) - len(ecg_filtered)), 'edge')
    
    return ecg_filtered


# =============================================================================
# CLASE: COMUNICACIÓN SERIAL
# =============================================================================

class XSpaceSerial:
    """Maneja comunicación con el dispositivo XSpace"""
    
    def __init__(self, port=None):
        self.port = port
        self.serial_conn = None
        
    def connect(self):
        """Conecta al puerto serial"""
        if self.port is None:
            self.port = HolterConfig.find_xspace_port()
            
        if self.port is None:
            port_input = input("\n[INPUT] Ingrese el puerto COM (ej: COM3): ").strip()
            self.port = port_input
        
        try:
            self.serial_conn = serial.Serial(
                port=self.port,
                baudrate=HolterConfig.BAUD_RATE,
                timeout=HolterConfig.SERIAL_TIMEOUT
            )
            time.sleep(2)  # Esperar reset del ESP32
            print(f"[OK] Conectado a {self.port} @ {HolterConfig.BAUD_RATE} baud")
            return True
        except Exception as e:
            print(f"[ERROR] No se pudo conectar a {self.port}: {e}")
            return False
    
    def read_line(self):
        """Lee una línea del serial"""
        try:
            if self.serial_conn.in_waiting > 0:
                line = self.serial_conn.readline().decode('utf-8', errors='ignore').strip()
                return line
        except Exception as e:
            print(f"[ERROR] Error leyendo serial: {e}")
        return None
    
    def send_command(self, command):
        """Envía un comando al dispositivo"""
        try:
            self.serial_conn.write(f"{command}\n".encode())
            print(f"[CMD] Enviado: {command}")
        except Exception as e:
            print(f"[ERROR] Error enviando comando: {e}")
    
    def wait_for_system_ready(self):
        """Espera a que el sistema esté listo"""
        print("[INFO] Esperando que el dispositivo esté listo...")
        while True:
            line = self.read_line()
            if line:
                print(f"[DEVICE] {line}")
                if line == "SYSTEM:READY":
                    return True
            time.sleep(0.1)
    
    def receive_data_block(self):
        """Recibe un bloque completo de datos desde el ESP32"""
        print("\n[INFO] Esperando transferencia de datos...")
        
        data_buffer = []
        num_samples = 0
        in_transfer = False
        
        while True:
            line = self.read_line()
            
            if line is None:
                time.sleep(0.01)
                continue
            
            # Mensajes del sistema
            if line.startswith("SYSTEM:") or line.startswith("ERROR:"):
                print(f"[DEVICE] {line}")
                continue
            
            # Progreso de captura
            if line.startswith("PROGRESS:"):
                print(f"[CAPTURE] {line.replace('PROGRESS:', '')}")
                continue
            
            # Inicio de captura
            if line == "CAPTURE:START":
                print("[CAPTURE] Iniciando captura de 15 segundos...")
                continue
            
            # Captura completa
            if line == "CAPTURE:COMPLETE":
                print("[CAPTURE] Captura completa. Iniciando transferencia...")
                continue
            
            # Inicio de transferencia
            if line == "TRANSFER:START":
                in_transfer = True
                data_buffer = []
                print("[TRANSFER] Recibiendo datos...")
                continue
            
            # Número de muestras
            if line.startswith("TRANSFER:SAMPLES:"):
                num_samples = int(line.replace("TRANSFER:SAMPLES:", ""))
                print(f"[TRANSFER] Esperando {num_samples} muestras...")
                continue
            
            # Fin de transferencia
            if line == "TRANSFER:END":
                print(f"[TRANSFER] Transferencia completa: {len(data_buffer)} muestras recibidas")
                return data_buffer
            
            # Datos
            if in_transfer and line.startswith("DATA:"):
                try:
                    data_str = line.replace("DATA:", "")
                    values = [float(x) for x in data_str.split(',')]
                    
                    if len(values) == 7:  # timestamp,ECG_I,ECG_II,ECG_III,AccX,AccY,AccZ
                        data_buffer.append(values)
                        
                        # Mostrar progreso cada 100 muestras
                        if len(data_buffer) % 100 == 0:
                            print(f"[TRANSFER] Recibidas: {len(data_buffer)}/{num_samples}")
                    else:
                        print(f"[WARNING] Línea con formato incorrecto: {len(values)} campos")
                        
                except ValueError as e:
                    print(f"[WARNING] Error parseando datos: {e}")
    
    def close(self):
        """Cierra la conexión serial"""
        if self.serial_conn:
            self.serial_conn.close()
            print("[INFO] Conexión serial cerrada")


# =============================================================================
# CLASE: PROCESADOR DE DATOS
# =============================================================================

class DataProcessor:
    """Procesa los datos capturados con wavelets"""
    
    def __init__(self, session_folder):
        self.session_folder = session_folder
        
    def process_data_block(self, data_buffer, block_number):
        """Procesa un bloque de datos completo"""
        
        if len(data_buffer) == 0:
            print("[WARNING] No hay datos para procesar")
            return
        
        print(f"\n{'='*70}")
        print(f"PROCESANDO BLOQUE #{block_number}")
        print(f"{'='*70}")
        
        # Convertir a DataFrame
        df = pd.DataFrame(data_buffer, columns=[
            'timestamp', 'ECG_I', 'ECG_II', 'ECG_III', 'AccX', 'AccY', 'AccZ'
        ])
        
        print(f"[INFO] Muestras totales: {len(df)}")
        print(f"[INFO] Duración: {(df['timestamp'].max() - df['timestamp'].min()) / 1000:.2f} segundos")
        
        # Calcular magnitud de aceleración
        df['AccMag'] = calculate_acceleration_magnitude(
            df['AccX'].values, 
            df['AccY'].values, 
            df['AccZ'].values
        )
        
        # Guardar datos crudos
        raw_file = os.path.join(self.session_folder, f"block_{block_number:03d}_raw.csv")
        df.to_csv(raw_file, index=False)
        print(f"[SAVE] Datos crudos: {raw_file}")
        
        # Detectar movimiento
        acc_threshold = np.percentile(df['AccMag'].values, HolterConfig.ACC_THRESHOLD_PERCENTILE)
        motion_mask = detect_motion_segments(df['AccMag'].values, acc_threshold)
        motion_percentage = np.mean(motion_mask) * 100
        
        print(f"[MOTION] Umbral: {acc_threshold:.3f} m/s²")
        print(f"[MOTION] Porcentaje de movimiento: {motion_percentage:.1f}%")
        
        # Aplicar filtrado wavelet a cada derivación
        print(f"[WAVELET] Aplicando filtrado adaptativo...")
        
        ecg_I_filt = adaptive_wavelet_filter(
            df['ECG_I'].values, 
            motion_mask, 
            HolterConfig.WAVELET_TYPE, 
            HolterConfig.DECOMPOSITION_LEVEL
        )
        
        ecg_II_filt = adaptive_wavelet_filter(
            df['ECG_II'].values, 
            motion_mask,
            HolterConfig.WAVELET_TYPE, 
            HolterConfig.DECOMPOSITION_LEVEL
        )
        
        ecg_III_filt = adaptive_wavelet_filter(
            df['ECG_III'].values, 
            motion_mask,
            HolterConfig.WAVELET_TYPE, 
            HolterConfig.DECOMPOSITION_LEVEL
        )
        
        # Crear DataFrame con datos filtrados
        df_filtered = pd.DataFrame({
            'timestamp': df['timestamp'],
            'ECG_I_filt': ecg_I_filt,
            'ECG_II_filt': ecg_II_filt,
            'ECG_III_filt': ecg_III_filt,
            'AccMag': df['AccMag'],
            'Motion': motion_mask.astype(int)
        })
        
        # Guardar datos filtrados
        filtered_file = os.path.join(self.session_folder, f"block_{block_number:03d}_filtered.csv")
        df_filtered.to_csv(filtered_file, index=False)
        print(f"[SAVE] Datos filtrados: {filtered_file}")
        
        print(f"{'='*70}\n")


# =============================================================================
# CLASE PRINCIPAL: SISTEMA HOLTER
# =============================================================================

class HolterSystem:
    """Sistema completo Holter - Modo captura por bloques"""
    
    def __init__(self, port=None):
        self.session_folder = HolterConfig.create_session_folder()
        self.serial = XSpaceSerial(port)
        self.processor = DataProcessor(self.session_folder)
        self.block_count = 0
        
    def start(self):
        """Inicia el sistema Holter"""
        print("="*70)
        print("SISTEMA HOLTER - MODO CAPTURA POR BLOQUES")
        print("="*70)
        print(f"Sesión: {self.session_folder}")
        print(f"Configuración:")
        print(f"  - Duración por captura: 15 segundos")
        print(f"  - Frecuencia de muestreo: {HolterConfig.SAMPLE_RATE} Hz")
        print(f"  - Wavelet: {HolterConfig.WAVELET_TYPE}")
        print("="*70)
        
        # Conectar al dispositivo
        if not self.serial.connect():
            print("[ERROR] No se pudo conectar al dispositivo")
            return
        
        # Esperar que el sistema esté listo
        self.serial.wait_for_system_ready()
        
        print("\n[INFO] Sistema listo. Presiona Ctrl+C para detener\n")
        
        try:
            while True:
                # Recibir bloque de datos (el ESP32 inicia captura automáticamente)
                data_block = self.serial.receive_data_block()
                
                if len(data_block) > 0:
                    self.block_count += 1
                    
                    # Procesar el bloque
                    self.processor.process_data_block(data_block, self.block_count)
                    
                    # Pedir nueva captura
                    print("[INFO] Enviando comando para nueva captura...")
                    self.serial.send_command("START")
                else:
                    print("[WARNING] No se recibieron datos")
                    break
                    
        except KeyboardInterrupt:
            print("\n[INFO] Deteniendo sistema...")
            self.stop()
    
    def stop(self):
        """Detiene el sistema"""
        self.serial.close()
        
        print("\n" + "="*70)
        print("SISTEMA HOLTER - FINALIZADO")
        print("="*70)
        print(f"Total de bloques procesados: {self.block_count}")
        print(f"Archivos guardados en: {self.session_folder}")
        print("="*70)


# =============================================================================
# PUNTO DE ENTRADA PRINCIPAL
# =============================================================================

if __name__ == "__main__":
    import sys
    
    port = None
    if len(sys.argv) > 1:
        port = sys.argv[1]
        print(f"[INFO] Usando puerto: {port}")
    
    holter = HolterSystem(port)
    holter.start()
