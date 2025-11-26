#include "holter_capture.h"
#include <time.h>
#include <SPI.h>

// ============================================================================
// CONFIGURACIÓN HARDWARE
// ============================================================================

#define SD_CS_PIN 5

// Pines SPI del ESP32 (por defecto)
#define SD_MOSI 23
#define SD_MISO 19
#define SD_SCK 18

// ============================================================================
// VARIABLES INTERNAS (PRIVADAS)
// ============================================================================

// Punteros a hardware
static XSpaceBioV10Board* g_bioBoard = nullptr;

// Configuración
static const int CAPTURE_DURATION_SEC = 15;
static const int ECG_SAMPLE_RATE_HZ = 250;
static const float ECG_SCALE_FACTOR = 6553.6;
static const int BUFFER_SIZE = 8192;

// Estado
static bool isCapturing = false;
static bool sdAvailable = false;

// Archivo actual
static File dataFile;
static String currentSessionFile = "";
static String currentSessionID = "";

// Contadores
static unsigned long captureStartTime = 0;
static unsigned long sampleCount = 0;

// Timing
static unsigned long lastECGSample = 0;
static const unsigned long ECG_INTERVAL_US = 1000000 / ECG_SAMPLE_RATE_HZ;

// Buffer de escritura
static uint8_t writeBuffer[BUFFER_SIZE];
static int bufferIndex = 0;
static unsigned long lastFlush = 0;

// ============================================================================
// FUNCIONES INTERNAS (PRIVADAS)
// ============================================================================

static void flushBuffer() {
  if (!sdAvailable || bufferIndex == 0) {
    bufferIndex = 0;
    return;
  }
  
  if (!dataFile) {
    Serial.println("[ERROR] Archivo no está abierto!");
    bufferIndex = 0;
    return;
  }
  
  size_t written = dataFile.write(writeBuffer, bufferIndex);
  
  if (written == 0) {
    Serial.println("[ERROR] Write failed - SD Card error!");
  } else if (written != bufferIndex) {
    Serial.printf("[WARNING] Escritura parcial: %d/%d bytes\n", written, bufferIndex);
  }
  
  bufferIndex = 0;
}

static void writeToBuffer(uint8_t* data, size_t len) {
  for (size_t i = 0; i < len; i++) {
    writeBuffer[bufferIndex++] = data[i];
    if (bufferIndex >= BUFFER_SIZE) {
      flushBuffer();
    }
  }
}

// ============================================================================
// IMPLEMENTACIÓN DE INTERFACE PÚBLICA
// ============================================================================

void holter_init(XSpaceBioV10Board* bioBoard, XSpaceV21Board* v21Board) {
  g_bioBoard = bioBoard;
  
  Serial.println("[INIT] Inicializando módulo de captura...");
  
  // Configurar pines SPI explícitamente
  pinMode(SD_CS_PIN, OUTPUT);
  digitalWrite(SD_CS_PIN, HIGH);
  
  delay(100);
  
  // Inicializar SPI con pines específicos
  SPI.begin(SD_SCK, SD_MISO, SD_MOSI, SD_CS_PIN);
  
  Serial.println("[INIT] SPI inicializado");
  Serial.printf("[INIT] Pines - CS:%d, MOSI:%d, MISO:%d, SCK:%d\n", 
                SD_CS_PIN, SD_MOSI, SD_MISO, SD_SCK);
  
  // Intentar montar SD Card
  Serial.print("[SD] Inicializando tarjeta SD...");
  
  SD.end();
  delay(500);
  
  bool sdMounted = false;
  for (int i = 0; i < 5 && !sdMounted; i++) {
    if (i > 0) {
      Serial.printf(" reintento %d...", i);
      delay(1000);
    }
    
    sdMounted = SD.begin(SD_CS_PIN, SPI, 4000000);
    
    if (!sdMounted) {
      SPI.end();
      delay(200);
      SPI.begin(SD_SCK, SD_MISO, SD_MOSI, SD_CS_PIN);
      delay(200);
    }
  }
  
  if (!sdMounted) {
    Serial.println(" [FAIL]");
    Serial.println("[ERROR] SD Card no disponible");
    sdAvailable = false;
  } else {
    Serial.println(" [OK]");
    
    uint8_t cardType = SD.cardType();
    if (cardType == CARD_NONE) {
      Serial.println("[WARNING] No se detectó tarjeta SD");
      sdAvailable = false;
    } else {
      Serial.print("[SD] Tipo: ");
      if (cardType == CARD_MMC) Serial.println("MMC");
      else if (cardType == CARD_SD) Serial.println("SDSC");
      else if (cardType == CARD_SDHC) Serial.println("SDHC");
      else Serial.println("UNKNOWN");
      
      uint64_t cardSize = SD.cardSize() / (1024 * 1024);
      Serial.printf("[SD] Tamaño: %lluMB\n", cardSize);
      
      uint64_t usedBytes = SD.usedBytes() / (1024 * 1024);
      uint64_t totalBytes = SD.totalBytes() / (1024 * 1024);
      Serial.printf("[SD] Usado: %lluMB / %lluMB\n", usedBytes, totalBytes);
      
      sdAvailable = true;
    }
  }
  
  Serial.println("[INIT] Módulo de captura listo");
}

bool holter_startCapture() {
  Serial.println("\n========================================");
  Serial.println("INICIANDO CAPTURA");
  Serial.println("========================================");
  
  captureStartTime = millis();
  
  // Obtener timestamp Unix real
  time_t now;
  time(&now);
  unsigned long timestamp = (unsigned long)now;
  
  currentSessionID = "session_" + String(timestamp);
  currentSessionFile = "/" + currentSessionID + ".bin";
  
  Serial.println("[INFO] Sesión: " + currentSessionID);
  Serial.println("[INFO] Archivo: " + currentSessionFile);
  Serial.println("[INFO] Timestamp Unix: " + String(timestamp));
  Serial.printf("[INFO] Duración configurada: %d segundos\n", CAPTURE_DURATION_SEC);
  
  if (!sdAvailable) {
    Serial.println("[ERROR] SD Card no disponible - no se puede capturar");
    sampleCount = 100;
    isCapturing = false;
    return false;
  }
  
  if (SD.cardType() == CARD_NONE) {
    Serial.println("[ERROR] Tarjeta SD removida o no detectada");
    sdAvailable = false;
    return false;
  }
  
  Serial.println("[SD] Creando archivo...");
  dataFile = SD.open(currentSessionFile.c_str(), FILE_WRITE);
  
  if(!dataFile) {
    Serial.println("[ERROR] No se pudo crear archivo en SD");
    return false;
  }
  
  Serial.println("[SD] Archivo abierto correctamente");
  
  // Escribir header INICIAL con contadores en 0
  FileHeader header = {0};
  header.magic = 0x45434744; // "ECGD"
  header.version = 1;
  header.device_id = 1;
  header.session_id = timestamp;
  header.timestamp_start = timestamp;
  header.ecg_sample_rate = ECG_SAMPLE_RATE_HZ;
  header.imu_sample_rate = 0;
  header.num_ecg_samples = 0;  // Se actualizará al final
  header.num_imu_samples = 0;
  
  size_t headerWritten = dataFile.write((uint8_t*)&header, sizeof(FileHeader));
  if (headerWritten != sizeof(FileHeader)) {
    Serial.printf("[ERROR] Header incompleto (%d/%d bytes)\n", 
                  headerWritten, sizeof(FileHeader));
    dataFile.close();
    return false;
  }
  
  dataFile.flush();
  Serial.printf("[SD] Header inicial escrito: %d bytes\n", headerWritten);
  
  sampleCount = 0;
  bufferIndex = 0;
  lastFlush = millis();
  isCapturing = true;
  lastECGSample = micros();
  
  Serial.println("[CAPTURE] Capturando...\n");
  return true;
}

void holter_captureLoop() {
  if (!isCapturing) return;
  
  unsigned long currentTime = micros();
  unsigned long elapsed = (millis() - captureStartTime) / 1000;
  
  if (elapsed >= CAPTURE_DURATION_SEC) {
    holter_stopCapture();
    return;
  }
  
  // Muestreo ECG a 250Hz
  while (currentTime - lastECGSample >= ECG_INTERVAL_US) {
    lastECGSample += ECG_INTERVAL_US;
    
    float derivationI = g_bioBoard->AD8232_GetVoltage(AD8232_XS1);
    float derivationII = g_bioBoard->AD8232_GetVoltage(AD8232_XS2);
    
    const float OFFSET = 1.65;
    const float AD8232_GAIN = 1100.0;
    
    float ecgI_mV = ((derivationI - OFFSET) * 1000.0) / AD8232_GAIN;
    float ecgII_mV = ((derivationII - OFFSET) * 1000.0) / AD8232_GAIN;
    float derivationIII = ecgII_mV - ecgI_mV;
    
    ECGSample sample;
    sample.derivation_I = (int16_t)(ecgI_mV * ECG_SCALE_FACTOR);
    sample.derivation_II = (int16_t)(ecgII_mV * ECG_SCALE_FACTOR);
    sample.derivation_III = (int16_t)(derivationIII * ECG_SCALE_FACTOR);
    
    writeToBuffer((uint8_t*)&sample, sizeof(ECGSample));
    sampleCount++;
    
    currentTime = micros();
  }
  
  // Flush periódico (cada 2 segundos)
  if (millis() - lastFlush >= 2000) {
    flushBuffer();
    if (dataFile) {
      dataFile.flush();
    }
    lastFlush = millis();
  }
  
  // Progreso cada 3 segundos
  static unsigned long lastReport = 0;
  if (elapsed > 0 && elapsed % 3 == 0 && elapsed != lastReport) {
    lastReport = elapsed;
    Serial.printf("[PROGRESS] %lus/%ds | ECG: %lu muestras (%.1f Hz)\n", 
                  elapsed, CAPTURE_DURATION_SEC, sampleCount,
                  (float)sampleCount / elapsed);
  }
  
  yield();
}

void holter_stopCapture() {
  if (!isCapturing) return;
  
  Serial.println("\n[CAPTURE] Finalizando captura...");
  isCapturing = false;
  
  if (!sdAvailable || !dataFile) {
    Serial.println("[WARNING] Captura sin archivo abierto");
    return;
  }
  
  // Flush final de datos
  Serial.printf("[DEBUG] Flush final del buffer (%d bytes pendientes)\n", bufferIndex);
  flushBuffer();
  dataFile.flush();
  
  unsigned long fileSize = dataFile.size();
  Serial.printf("[DEBUG] Tamaño antes de cerrar: %lu bytes\n", fileSize);
  Serial.printf("[DEBUG] Muestras capturadas: %lu\n", sampleCount);
  
  // NO cerrar el archivo, solo hacer seek para actualizar header
  Serial.println("[DEBUG] Actualizando header sin cerrar archivo...");
  delay(100);

  const size_t OFFSET_NUM_ECG = 20;
  const size_t OFFSET_NUM_IMU = 24;
  
  // Escribir num_ecg_samples
  dataFile.seek(OFFSET_NUM_ECG);
  uint32_t ecg_count = (uint32_t)sampleCount;
  size_t written1 = dataFile.write((uint8_t*)&ecg_count, sizeof(uint32_t));
  
  // Escribir num_imu_samples (0)
  dataFile.seek(OFFSET_NUM_IMU);
  uint32_t imu_count = 0;
  size_t written2 = dataFile.write((uint8_t*)&imu_count, sizeof(uint32_t));
  
  if (written1 != sizeof(uint32_t) || written2 != sizeof(uint32_t)) {
    Serial.println("[ERROR] No se pudo actualizar contadores en header");
  } else {
    Serial.println("[DEBUG] Contadores actualizados en header:");
    Serial.printf("  - num_ecg_samples: %lu\n", sampleCount);
    Serial.printf("  - num_imu_samples: 0\n");
  }
  
  dataFile.flush();
  
  // Verificación final
  delay(100);
  
  File checkFile = SD.open(currentSessionFile.c_str(), FILE_READ);
  if (!checkFile) {
    Serial.println("[ERROR] No se pudo reabrir para verificación");
    return;
  }
  
  unsigned long finalSize = checkFile.size();
  
  // Leer y verificar header
  FileHeader verifyHeader;
  size_t headerRead = checkFile.read((uint8_t*)&verifyHeader, sizeof(FileHeader));
  checkFile.close();
  
  unsigned long expectedSize = sizeof(FileHeader) + (sampleCount * sizeof(ECGSample));
  
  Serial.println("\n========================================");
  Serial.println("CAPTURA COMPLETADA");
  Serial.println("========================================");
  Serial.printf("[INFO] Archivo: %s\n", currentSessionFile.c_str());
  Serial.printf("[INFO] Tamaño: %lu bytes (%.2f KB)\n", finalSize, finalSize/1024.0);
  Serial.printf("[INFO] ECG muestras: %lu\n", sampleCount);
  Serial.printf("[INFO] Frecuencia real: %.1f Hz\n", 
                (float)sampleCount / CAPTURE_DURATION_SEC);
  
  if (headerRead == sizeof(FileHeader)) {
    Serial.printf("[VERIFY] Header magic: 0x%08X\n", verifyHeader.magic);
    Serial.printf("[VERIFY] Header num_ecg: %u\n", verifyHeader.num_ecg_samples);
    Serial.printf("[VERIFY] Header num_imu: %u\n", verifyHeader.num_imu_samples);
    
    if (verifyHeader.num_ecg_samples == sampleCount) {
      Serial.println("[OK] Header actualizado correctamente ✓");
    } else {
      Serial.printf("[WARNING] Header no coincide: esperado %lu, leído %u\n", 
                    sampleCount, verifyHeader.num_ecg_samples);
    }
  } else {
    Serial.println("[ERROR] No se pudo leer header para verificar");
  }
  
  Serial.printf("[VERIFY] Esperado: %lu bytes | Real: %lu bytes\n", expectedSize, finalSize);
  
  if (finalSize == expectedSize) {
    Serial.println("[OK] Archivo completo y válido ✓");
  } else {
    long diff = (long)(finalSize - expectedSize);
    Serial.printf("[INFO] Diferencia: %ld bytes\n", diff);
  }
  Serial.println("========================================\n");
}

bool holter_isCapturing() {
  return isCapturing;
}

float holter_getProgress() {
  if (!isCapturing) return 0.0;
  unsigned long elapsed = (millis() - captureStartTime) / 1000;
  float progress = (float)elapsed / (float)CAPTURE_DURATION_SEC;
  return constrain(progress, 0.0, 1.0);
}

unsigned long holter_getElapsedSeconds() {
  if (!isCapturing) return 0;
  return (millis() - captureStartTime) / 1000;
}

String holter_getCurrentFile() {
  return currentSessionFile;
}

unsigned long holter_getECGSampleCount() {
  return sampleCount;
}

unsigned long holter_getIMUSampleCount() {
  return 0;
}

bool holter_isSDAvailable() {
  return sdAvailable;
}

bool holter_isIMUAvailable() {
  return false;
}