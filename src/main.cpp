#include <Arduino.h>
#include <XSpaceBioV10.h>
#include <XSpaceV21.h>
#include <Wire.h>
#include <SD.h>
#include <FS.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <PubSubClient.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <time.h>
#include "aws_config.h"

// ============================================================================
// OBJETOS PRINCIPALES
// ============================================================================
XSpaceBioV10Board MyBioBoard;
XSpaceV21Board XSBoard;  // Para BMI088
WiFiClientSecure wifiClient;
PubSubClient mqttClient(wifiClient);

// ============================================================================
// CONFIGURACIÓN SISTEMA 
// ============================================================================
const int CAPTURE_DURATION_SEC = 15;
const int ECG_SAMPLE_RATE_HZ = 250;  // Aumentado para mejor detección de QRS
const int IMU_SAMPLE_RATE_HZ = 50;   // Reducido a 50Hz para minimizar I2C
const unsigned long BAUD_RATE = 115200;
const unsigned long MAX_ECG_SAMPLES = (unsigned long)ECG_SAMPLE_RATE_HZ * CAPTURE_DURATION_SEC;
const unsigned long MAX_IMU_SAMPLES = (unsigned long)IMU_SAMPLE_RATE_HZ * CAPTURE_DURATION_SEC;

#define SD_CS_PIN 5

// Escalado para conversión float->int16
// Rango ECG típico: ±5 mV → Resolución: 5mV/32768 = 0.15 uV
const float ECG_SCALE_FACTOR = 6553.6;  // 32768 / 5.0 mV

// ============================================================================
// ESTADOS DEL SISTEMA
// ============================================================================
enum SystemState {
  STATE_INIT,
  STATE_CAPTURING,
  STATE_UPLOAD_REQUEST,
  STATE_UPLOADING,
  STATE_COMPLETE,
  STATE_ERROR
};

SystemState currentState = STATE_INIT;

// ============================================================================
// ESTRUCTURA DE ARCHIVO BINARIO
// ============================================================================
struct FileHeader {
  uint32_t magic;              // 0x45434744 = "ECGD"
  uint16_t version;
  uint16_t device_id;
  uint32_t session_id;
  uint32_t timestamp_start;
  uint16_t ecg_sample_rate;
  uint16_t imu_sample_rate;
  uint32_t num_ecg_samples;
  uint32_t num_imu_samples;
} __attribute__((packed));

// Estructura para muestra ECG (int16 en lugar de float)
struct ECGSample {
  int16_t derivation_I;
  int16_t derivation_II;
  int16_t derivation_III;
} __attribute__((packed));

// Estructura IMU solo acelerómetro
struct IMUSample {
  int16_t accel_x;
  int16_t accel_y;
  int16_t accel_z;
} __attribute__((packed));

// ============================================================================
// VARIABLES GLOBALES
// ============================================================================
File dataFile;
String currentSessionFile = "";
String currentSessionGzFile = "";
String currentSessionID = "";
unsigned long captureStartTime = 0;
unsigned long sampleCount = 0;
unsigned long imuSampleCount = 0;
bool isCapturing = false;
bool imuAvailable = false;
bool sdAvailable = false;

// Timing
unsigned long lastECGSample = 0;
unsigned long lastIMUSample = 0;
const unsigned long ECG_INTERVAL_US = 1000000 / ECG_SAMPLE_RATE_HZ;
const unsigned long IMU_INTERVAL_US = 1000000 / IMU_SAMPLE_RATE_HZ;

// Buffer optimizado (8KB para ~2.7s de datos)
const int BUFFER_SIZE = 8192;
uint8_t writeBuffer[BUFFER_SIZE];
int bufferIndex = 0;
unsigned long lastFlush = 0;

// Upload
String uploadURL = "";
bool urlReceived = false;
unsigned long uploadStartTime = 0;
const unsigned long UPLOAD_TIMEOUT_MS = 30000; // milisegundos

// NTP
const char* ntpServer = "pool.ntp.org";
const long gmtOffset_sec = -5 * 3600;  // UTC-5 (Perú)
const int daylightOffset_sec = 0;

// ============================================================================
// FUNCIONES WIFI
// ============================================================================
void syncTime() {
  Serial.println("[NTP] Sincronizando hora...");
  configTime(gmtOffset_sec, daylightOffset_sec, ntpServer);
  
  struct tm timeinfo;
  if(!getLocalTime(&timeinfo)){
    Serial.println("[WARNING] No se pudo obtener hora NTP");
    return;
  }
  
  Serial.printf("[NTP] Hora sincronizada: %02d/%02d/%04d %02d:%02d:%02d\n",
                timeinfo.tm_mday, timeinfo.tm_mon + 1, timeinfo.tm_year + 1900,
                timeinfo.tm_hour, timeinfo.tm_min, timeinfo.tm_sec);
}

void connectWiFi() {
  Serial.println("\n[WiFi] Conectando a: " + String(WIFI_SSID));
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  
  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 20) {
    delay(500);
    Serial.print(".");
    attempts++;
  }
  
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\n[WiFi] Conectado");
    Serial.println("[WiFi] IP: " + WiFi.localIP().toString());
    Serial.println("[WiFi] RSSI: " + String(WiFi.RSSI()) + " dBm");
    syncTime();  // Sincronizar hora NTP
  } else {
    Serial.println("\n[WiFi] ERROR: No se pudo conectar");
    currentState = STATE_ERROR;
  }
}

void disconnectWiFi() {
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  Serial.println("[WiFi] Desconectado (ahorro energía)");
}

// ============================================================================
// FUNCIONES MQTT
// ============================================================================
void mqttCallback(char* topic, byte* payload, unsigned int length) {
  Serial.println("\n[MQTT] ========== MENSAJE RECIBIDO ==========");
  Serial.println("[MQTT] Topic: " + String(topic));
  Serial.print("[MQTT] Payload: ");
  for (unsigned int i = 0; i < length; i++) {
    Serial.print((char)payload[i]);
  }
  Serial.println();
  
  // Parsear JSON
  DynamicJsonDocument doc(1024);
  DeserializationError error = deserializeJson(doc, payload, length);
  
  if (error) {
    Serial.println("[ERROR] JSON parsing failed: " + String(error.c_str()));
    return;
  }
  
  Serial.println("[DEBUG] JSON parseado correctamente");
  
  if (String(topic) == TOPIC_RESPONSE) {
    Serial.println("[DEBUG] Topic coincide con TOPIC_RESPONSE");
    if (doc.containsKey("upload_url")) {
      uploadURL = doc["upload_url"].as<String>();
      urlReceived = true;
      Serial.println("[MQTT] URL recibida: " + uploadURL.substring(0, 50) + "...");
    } else {
      Serial.println("[WARNING] JSON no contiene 'upload_url'");
      serializeJsonPretty(doc, Serial);
      Serial.println();
    }
  } else {
    Serial.println("[WARNING] Topic no coincide. Esperado: " + String(TOPIC_RESPONSE));
  }
  Serial.println("[MQTT] ==========================================\n");
}

bool connectMQTT() {
  Serial.println("[MQTT] Configurando AWS IoT...");
  
  // Certificados
  wifiClient.setCACert(AWS_CERT_CA);
  wifiClient.setCertificate(AWS_CERT_CRT);
  wifiClient.setPrivateKey(AWS_CERT_PRIVATE);
  
  // IMPORTANTE: setBufferSize ANTES de setServer
  mqttClient.setBufferSize(4096);
  Serial.println("[DEBUG] Buffer MQTT configurado: 4096 bytes");
  
  mqttClient.setServer(AWS_IOT_ENDPOINT, AWS_IOT_PORT);
  mqttClient.setCallback(mqttCallback);
  mqttClient.setKeepAlive(60);  // 60 segundos keepAlive
  
  Serial.println("[MQTT] Conectando a AWS IoT Core...");
  Serial.println("[DEBUG] KeepAlive: 60s");
  
  int attempts = 0;
  while (!mqttClient.connected() && attempts < 3) {
    // Conectar con clean session y último will
    if (mqttClient.connect(DEVICE_ID, NULL, NULL, NULL, 0, false, NULL, true)) {
      Serial.println("[MQTT] Conectado a AWS IoT Core");
      
      // Verificar que sigue conectado
      delay(100);
      if (!mqttClient.connected()) {
        Serial.println("[ERROR] Conexión perdida inmediatamente");
        attempts++;
        continue;
      }
      
      // Suscribirse al topic específico con QoS 1
      if (mqttClient.subscribe(TOPIC_RESPONSE, 1)) {
        Serial.println("[MQTT] Suscrito a: " + String(TOPIC_RESPONSE) + " (QoS 1)");
      } else {
        Serial.println("[ERROR] No se pudo suscribir a: " + String(TOPIC_RESPONSE));
        attempts++;
        continue;
      }
      
      // Mantener conexión activa mientras esperamos
      Serial.println("[MQTT] Esperando confirmación de suscripción...");
      for (int i = 0; i < 20; i++) {
        mqttClient.loop();  // Mantener keepAlive
        if (!mqttClient.connected()) {
          Serial.println("[ERROR] Conexión perdida durante suscripción");
          attempts++;
          break;
        }
        delay(50);
      }
      
      if (mqttClient.connected()) {
        Serial.println("[MQTT] Listo para recibir mensajes");
        return true;
      }
    } else {
      Serial.println("[MQTT] Error conectando: " + String(mqttClient.state()));
      attempts++;
      delay(2000);
    }
  }
  
  Serial.println("[MQTT] Falló después de 3 intentos");
  return false;
}

void requestUploadURL() {
  Serial.println("\n[UPLOAD] Solicitando URL de AWS...");
  
  unsigned long fileSize = 0;
  
  if (sdAvailable) {
    File file = SD.open(currentSessionFile.c_str(), FILE_READ);
    if (!file) {
      Serial.println("[ERROR] No se pudo abrir archivo");
      currentState = STATE_ERROR;
      return;
    }
    fileSize = file.size();
    file.close();
  } else {
    // Modo prueba: simular tamaño de archivo
    fileSize = sizeof(FileHeader) + (sampleCount * sizeof(ECGSample)) + (imuSampleCount * sizeof(IMUSample));
    Serial.printf("[INFO] Tamaño simulado: %lu bytes (%.2f KB)\n", fileSize, fileSize / 1024.0);
  }
  
  // Crear payload JSON
  DynamicJsonDocument doc(512);
  doc["device_id"] = DEVICE_ID;
  doc["session_id"] = currentSessionID;
  doc["timestamp"] = String(captureStartTime / 1000);
  doc["file_size"] = fileSize;
  doc["ready_for_upload"] = true;
  
  char jsonBuffer[512];
  size_t jsonSize = serializeJson(doc, jsonBuffer);
  
  Serial.println("[MQTT] Publicando solicitud...");
  Serial.println("[DEBUG] Topic: " + String(TOPIC_REQUEST));
  Serial.println("[DEBUG] Payload size: " + String(jsonSize) + " bytes");
  Serial.println("[DEBUG] Payload: " + String(jsonBuffer));
  Serial.println("[DEBUG] Esperando en: " + String(TOPIC_RESPONSE));
  
  mqttClient.loop();
  
  // Publicar como byte array con longitud explícita
  bool publishResult = mqttClient.publish(TOPIC_REQUEST, (uint8_t*)jsonBuffer, jsonSize);
  
  Serial.println("[DEBUG] publish() retornó: " + String(publishResult ? "true" : "false"));
  
  if (publishResult) {
    Serial.println("[MQTT] Solicitud enviada");
    Serial.println("[INFO] Esperando respuesta (60s timeout)...");
    
    uploadStartTime = millis();
    urlReceived = false;
    
    // Esperar respuesta con logging cada 5 segundos
    unsigned long lastLog = millis();
    while (!urlReceived && (millis() - uploadStartTime) < 60000) {
      mqttClient.loop();
      
      // Log cada 5 segundos
      if (millis() - lastLog > 5000) {
        Serial.printf("[WAIT] Esperando... (%lus)\n", (millis() - uploadStartTime) / 1000);
        lastLog = millis();
      }
      
      delay(100);
    }
    
    if (urlReceived) {
      currentState = STATE_UPLOADING;
    } else {
      Serial.println("[ERROR] Timeout esperando URL (60s)");
      currentState = STATE_ERROR;
    }
  } else {
    Serial.println("[ERROR] No se pudo publicar - Estado: " + String(mqttClient.state()));
    currentState = STATE_ERROR;
  }
}

// ============================================================================
// FUNCIONES HTTP UPLOAD
// ============================================================================
bool uploadToS3() {
  Serial.println("\n[S3] Iniciando upload...");
  
  File file = SD.open(currentSessionFile.c_str(), FILE_READ);
  if (!file) {
    Serial.println("[ERROR] No se pudo abrir archivo");
    return false;
  }
  
  unsigned long fileSize = file.size();
  Serial.println("[S3] Archivo: " + currentSessionFile);
  Serial.println("[S3] Tamaño: " + String(fileSize / 1024) + " KB");
  
  // Leer archivo completo en memoria (es pequeño, ~18KB)
  uint8_t* fileData = (uint8_t*)malloc(fileSize);
  if (!fileData) {
    Serial.println("[ERROR] No hay memoria suficiente");
    file.close();
    return false;
  }
  
  Serial.println("[S3] Leyendo archivo...");
  size_t bytesRead = file.read(fileData, fileSize);
  file.close();
  
  if (bytesRead != fileSize) {
    Serial.println("[ERROR] Lectura incompleta");
    free(fileData);
    return false;
  }
  
  Serial.println("[S3] Conectando a S3...");
  HTTPClient http;
  http.begin(uploadURL);
  http.addHeader("Content-Type", "application/octet-stream");
  http.addHeader("Content-Length", String(fileSize));
  http.setTimeout(30000);  // 30 segundos timeout
  
  Serial.println("[S3] Enviando datos...");
  int httpCode = http.PUT(fileData, fileSize);
  
  free(fileData);
  
  Serial.println("[S3] HTTP Code: " + String(httpCode));
  
  if (httpCode == 200 || httpCode == 204) {
    Serial.println("[S3] Upload exitoso!");
    http.end();
    
    // Borrar archivo de SD
    if (SD.remove(currentSessionFile.c_str())) {
      Serial.println("[SD] Archivo eliminado (espacio liberado)");
    }
    
    return true;
  } else {
    Serial.println("[S3] Error HTTP: " + String(httpCode));
    String response = http.getString();
    Serial.println("[S3] Response: " + response);
    http.end();
    return false;
  }
}

// ============================================================================
// FUNCIONES DE CAPTURA (igual que Fase 1)
// ============================================================================
// Forward declarations
void stopCapture();

void flushBuffer() {
  if (sdAvailable && bufferIndex > 0 && dataFile) {
    size_t written = dataFile.write(writeBuffer, bufferIndex);
    // NO hacer flush aquí - dejarlo para el final
    
    static unsigned long totalWritten = 0;
    totalWritten += written;
    
    Serial.printf("[FLUSH] Escribió %d bytes (total: %lu bytes)\n", written, totalWritten);
    
    if (written == 0) {
      Serial.println("[ERROR] Write failed!");
    } else if (written != bufferIndex) {
      Serial.printf("[WARNING] Escritura parcial: %d/%d bytes\n", written, bufferIndex);
    }
    
    bufferIndex = 0;
  }
}

void writeToBuffer(uint8_t* data, size_t len) {
  for (size_t i = 0; i < len; i++) {
    writeBuffer[bufferIndex++] = data[i];
    if (bufferIndex >= BUFFER_SIZE) {
      flushBuffer();
    }
  }
}

void startCapture() {
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
  currentSessionGzFile = "/" + currentSessionID + ".bin.gz";
  
  Serial.println("[INFO] Sesión: " + currentSessionID);
  Serial.println("[INFO] Archivo: " + currentSessionFile);
  Serial.println("[INFO] Timestamp Unix: " + String(timestamp));
  Serial.printf("[INFO] Duración configurada: %d segundos\n", CAPTURE_DURATION_SEC);
  
  if (!sdAvailable) {
    Serial.println("[WARNING] Modo prueba - saltando captura");
    // En modo prueba, simular datos mínimos
    sampleCount = 100;  // Simular 100 muestras
    imuSampleCount = 100;
    isCapturing = false;
    Serial.println("[CAPTURE] Captura simulada instantánea\n");
    currentState = STATE_UPLOAD_REQUEST;  // Ir directo a upload
    return;
  }
  
  dataFile = SD.open(currentSessionFile.c_str(), FILE_WRITE);
  if(!dataFile) {
    Serial.println("[ERROR] No se pudo crear archivo");
    currentState = STATE_ERROR;
    return;
  }
  
  Serial.println("[SD] Archivo abierto correctamente");
  
  if (sdAvailable) {
    FileHeader header = {0};
    header.magic = 0x45434744;  // "ECGD"
    header.version = 1;
    header.device_id = 1;
    header.session_id = timestamp;
    header.timestamp_start = timestamp;
    header.ecg_sample_rate = ECG_SAMPLE_RATE_HZ;
    header.imu_sample_rate = IMU_SAMPLE_RATE_HZ;
    
    dataFile.write((uint8_t*)&header, sizeof(FileHeader));
    dataFile.flush();  // Asegurar que header se escriba
  }
  
  sampleCount = 0;
  imuSampleCount = 0;
  bufferIndex = 0;
  lastFlush = millis();
  isCapturing = true;
  
  lastECGSample = micros();
  lastIMUSample = micros();
  
  currentState = STATE_CAPTURING;
  
  Serial.println("[CAPTURE] Capturando...\n");
}

void captureLoop() {
  unsigned long currentTime = micros();
  unsigned long elapsed = (millis() - captureStartTime) / 1000;
  
  if (elapsed >= CAPTURE_DURATION_SEC) {
    stopCapture();
    return;
  }
  
  // ECG - PRIORIDAD ALTA (debe ejecutarse siempre a tiempo)
  while (currentTime - lastECGSample >= ECG_INTERVAL_US) {
    lastECGSample += ECG_INTERVAL_US;
    
    float derivationI = MyBioBoard.AD8232_GetVoltage(AD8232_XS1);
    float derivationII = MyBioBoard.AD8232_GetVoltage(AD8232_XS2);
    
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
    
    currentTime = micros();  // Actualizar tiempo
  }
  
  // IMU - DESACTIVADO TEMPORALMENTE PARA DEBUG
  /*
  if (currentTime - lastIMUSample >= IMU_INTERVAL_US) {
    lastIMUSample += IMU_INTERVAL_US;
    
    IMUSample sample = {0};
    
    if (imuAvailable) {
      float ax, ay, az;
      XSBoard.BMI088_GetAccelData(&ax, &ay, &az);
      
      sample.accel_x = (int16_t)(ax * 2048.0);
      sample.accel_y = (int16_t)(ay * 2048.0);
      sample.accel_z = (int16_t)(az * 2048.0);
    }
    
    writeToBuffer((uint8_t*)&sample, sizeof(IMUSample));
    imuSampleCount++;
  }
  */
  
  // Flush periódico cada 3 segundos para reducir operaciones SD
  if (millis() - lastFlush >= 3000) {
    flushBuffer();
    lastFlush = millis();
  }
  
  // Progreso cada 3 segundos
  static unsigned long lastReport = 0;
  if (elapsed > 0 && elapsed % 3 == 0 && elapsed != lastReport) {
    lastReport = elapsed;
    Serial.printf("[PROGRESS] %lus/%ds", // | ECG: %lu | IMU: %lu\n
                  elapsed, CAPTURE_DURATION_SEC, sampleCount, imuSampleCount);
  }
  
  yield();
}

void stopCapture() {
  if (!isCapturing) return;
  
  Serial.println("\n[CAPTURE] Finalizando...");
  
  isCapturing = false;
  
  if (!sdAvailable) {
    // Modo prueba sin SD
    Serial.println("\n========================================");
    Serial.println("CAPTURA SIMULADA COMPLETADA");
    Serial.println("========================================");
    Serial.printf("[INFO] ECG: %lu muestras (simuladas)\n", sampleCount);
    Serial.printf("[INFO] IMU: %lu muestras (simuladas)\n", imuSampleCount);
    Serial.println("[INFO] Pasando a solicitar URL de AWS...");
    Serial.println("========================================\n");
    currentState = STATE_UPLOAD_REQUEST;
    return;
  }
  
  // CRÍTICO: Flush final del buffer
  Serial.printf("[DEBUG] Buffer antes de flush: %d bytes\n", bufferIndex);
  flushBuffer();
  Serial.println("[DEBUG] Buffer flushed");
  
  // FLUSH ÚNICO al filesystem para escribir todo a SD
  Serial.println("[DEBUG] Haciendo flush al filesystem...");
  dataFile.flush();
  Serial.println("[DEBUG] Flush completado");
  
  // Verificar tamaño actual del archivo antes de actualizar header
  unsigned long currentSize = dataFile.size();
  Serial.printf("[DEBUG] Tamaño archivo antes de cerrar: %lu bytes\n", currentSize);
  
  // Actualizar header con contadores finales
  dataFile.seek(0);
  FileHeader header;
  dataFile.read((uint8_t*)&header, sizeof(FileHeader));
  header.num_ecg_samples = sampleCount;
  header.num_imu_samples = imuSampleCount;
  
  dataFile.seek(0);
  dataFile.write((uint8_t*)&header, sizeof(FileHeader));
  dataFile.flush();
  
  // Verificar tamaño final
  unsigned long finalSize = dataFile.size();
  Serial.printf("[DEBUG] Tamaño archivo después de header: %lu bytes\n", finalSize);
  
  dataFile.close();
  Serial.println("[DEBUG] Archivo cerrado");
  
  // Verificar con nueva apertura
  File checkFile = SD.open(currentSessionFile.c_str(), FILE_READ);
  if (!checkFile) {
    Serial.println("[ERROR] No se pudo reabrir archivo para verificar");
    currentState = STATE_ERROR;
    return;
  }
  
  unsigned long fileSize = checkFile.size();
  checkFile.close();
  
  // Calcular tamaño esperado
  unsigned long expectedSize = sizeof(FileHeader) + 
                               (sampleCount * sizeof(ECGSample)) + 
                               (imuSampleCount * sizeof(IMUSample));
  
  Serial.println("\n========================================");
  Serial.println("CAPTURA COMPLETADA");
  Serial.println("========================================");
  Serial.printf("[INFO] Archivo: %lu KB (%.2f MB)\n", fileSize/1024, fileSize/(1024.0*1024.0));
  Serial.printf("[INFO] ECG: %lu muestras (%.1f Hz)\n", 
                sampleCount, (float)sampleCount / CAPTURE_DURATION_SEC);
  Serial.printf("[INFO] IMU: %lu muestras (%.1f Hz)\n",
                imuSampleCount, (float)imuSampleCount / CAPTURE_DURATION_SEC);
  Serial.printf("[VERIFY] Esperado: %lu bytes | Real: %lu bytes\n", expectedSize, fileSize);
  
  if (fileSize < sizeof(FileHeader)) {
    Serial.println("[ERROR] Archivo corrupto - solo header o vacío");
    currentState = STATE_ERROR;
    return;
  }
  
  if (fileSize == expectedSize) {
    Serial.println("[OK] Archivo completo y válido");
  } else {
    Serial.printf("[WARNING] Diferencia: %ld bytes\n", (long)(fileSize - expectedSize));
  }
  Serial.println("========================================\n");
  
  currentState = STATE_UPLOAD_REQUEST;
}

// ============================================================================
// SETUP
// ============================================================================
void setup() {
  Serial.begin(BAUD_RATE);
  delay(2000);
  
  Serial.println("\n========================================");
  Serial.println("HOLTER FASE 2: CAPTURA + AWS UPLOAD");
  Serial.println("========================================");
  
  // Hardware
  MyBioBoard.init();
  MyBioBoard.AD8232_Wake(AD8232_XS1);
  MyBioBoard.AD8232_Wake(AD8232_XS2);
  Serial.println("[OK] XSpaceBio + ECG");
  
  Wire.begin();
  XSBoard.BMI088_init(16, 17);  // Inicializar BMI088
  
  // Probar lectura para verificar si funciona
  float ax, ay, az;
  XSBoard.BMI088_GetAccelData(&ax, &ay, &az);
  
  if (ax == 0 && ay == 0 && az == 0) {
    Serial.println("[WARNING] BMI088 no detectado - usando datos simulados (0)");
    imuAvailable = false;
  } else {
    Serial.println("[OK] BMI088");
    imuAvailable = true;
  }
  
  if(!SD.begin(SD_CS_PIN)) {
    Serial.println("[WARNING] SD Card no detectada - modo prueba AWS (sin captura real)");
    sdAvailable = false;
  } else {
    Serial.println("[OK] SD Card");
    sdAvailable = true;
  }
  
  Serial.println("\n[INFO] WiFi desconectado durante captura");
  Serial.println("[INFO] Se conectará después para upload");
  Serial.println("\n[READY] Iniciando captura en 3 segundos...\n");
  
  delay(3000);
  
  startCapture();
}

// ============================================================================
// LOOP
// ============================================================================
void loop() {
  switch (currentState) {
    case STATE_CAPTURING:
      captureLoop();
      break;
      
    case STATE_UPLOAD_REQUEST:
      connectWiFi();
      if (WiFi.status() == WL_CONNECTED) {
        if (connectMQTT()) {
          requestUploadURL();
        } else {
          currentState = STATE_ERROR;
        }
      }
      break;
      
    case STATE_UPLOADING:
      if (!sdAvailable) {
        Serial.println("\n[INFO] Modo prueba - no hay archivo para subir");
        Serial.println("[SUCCESS] Comunicación MQTT con AWS completada");
        Serial.println("========================================\n");
        currentState = STATE_COMPLETE;
      } else if (uploadToS3()) {
        Serial.println("\n========================================");
        Serial.println("SESIÓN COMPLETADA EXITOSAMENTE");
        Serial.println("========================================\n");
        currentState = STATE_COMPLETE;
      } else {
        currentState = STATE_ERROR;
      }
      break;
      
    case STATE_COMPLETE:
      disconnectWiFi();
      Serial.println("[INFO] Reiniciando en 10 segundos...\n");
      delay(10000);
      ESP.restart();
      break;
      
    case STATE_ERROR:
      Serial.println("\n[ERROR] Error en el sistema");
      Serial.println("[INFO] Reiniciando en 30 segundos...\n");
      delay(30000);
      ESP.restart();
      break;
  }
  
  if (mqttClient.connected()) {
    mqttClient.loop();
  }
  
  yield();
}
