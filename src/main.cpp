#include <Arduino.h>
#include <XSpaceBioV10.h>
#include <Wire.h>
#include <Adafruit_ADXL345_U.h>

// ============================================================================
// OBJETOS PRINCIPALES
// ============================================================================
XSpaceBioV10Board MyBioBoard;
Adafruit_ADXL345_Unified accel = Adafruit_ADXL345_Unified(12345);

// ============================================================================
// CONFIGURACIÓN
// ============================================================================
const int SAMPLE_RATE_HZ = 100;         // Frecuencia de muestreo objetivo
const int SAMPLE_INTERVAL_US = 10000;   // 10ms en microsegundos
const unsigned long BAUD_RATE = 115200; // Velocidad serial USB
const int CAPTURE_DURATION_SEC = 15;    // Duración de captura en segundos
const int MAX_SAMPLES = SAMPLE_RATE_HZ * CAPTURE_DURATION_SEC; // 1500 muestras

// Arrays para almacenar datos
double DerivationI[MAX_SAMPLES];
double DerivationII[MAX_SAMPLES];
double DerivationIII[MAX_SAMPLES];
double AccelX[MAX_SAMPLES];
double AccelY[MAX_SAMPLES];
double AccelZ[MAX_SAMPLES];
unsigned long Timestamps[MAX_SAMPLES];

// Variables de control
int currentSample = 0;
unsigned long lastSampleTime = 0;
bool isCapturing = false;
bool dataReady = false;

void setup() {
  
  // Inicializar Serial USB para transmisión de datos
  Serial.begin(BAUD_RATE);
  delay(2000); // Esperar conexión USB
  
  Serial.println("SYSTEM:STARTING");
  
  // Inicializar XSpaceBio
  MyBioBoard.init();
  Serial.println("SYSTEM:XSPACEBIO_OK");
  
  // Activar sensores ECG
  MyBioBoard.AD8232_Wake(AD8232_XS1);
  MyBioBoard.AD8232_Wake(AD8232_XS2);
  
  // Inicializar I2C
  Wire.begin();
  
  // Inicializar ADXL345
  if(!accel.begin()) {
    Serial.println("ERROR:ADXL345_NOT_FOUND");
    while(1) {
      delay(1000);
      Serial.println("ERROR:ADXL345_NOT_FOUND");
    }
  }
  
  // Configurar ADXL345
  accel.setRange(ADXL345_RANGE_4_G);
  accel.setDataRate(ADXL345_DATARATE_100_HZ);
  Serial.println("SYSTEM:ADXL345_OK");
  
  Serial.println("SYSTEM:READY");
  Serial.print("SYSTEM:BUFFER_SIZE:");
  Serial.println(MAX_SAMPLES);
  
  delay(1000);
  
  // Iniciar primera captura
  startCapture();
}

void startCapture() {
  currentSample = 0;
  isCapturing = true;
  dataReady = false;
  lastSampleTime = micros();
  
  Serial.println("CAPTURE:START");
}

void sendDataToPC() {
  Serial.println("TRANSFER:START");
  Serial.print("TRANSFER:SAMPLES:");
  Serial.println(currentSample);
  
  // Enviar datos en formato CSV compacto
  for (int i = 0; i < currentSample; i++) {
    Serial.print("DATA:");
    Serial.print(Timestamps[i]);
    Serial.print(",");
    Serial.print(DerivationI[i], 6);
    Serial.print(",");
    Serial.print(DerivationII[i], 6);
    Serial.print(",");
    Serial.print(DerivationIII[i], 6);
    Serial.print(",");
    Serial.print(AccelX[i], 4);
    Serial.print(",");
    Serial.print(AccelY[i], 4);
    Serial.print(",");
    Serial.println(AccelZ[i], 4);
  }
  
  Serial.println("TRANSFER:END");
  dataReady = false;
}

void loop() {
  
  // Si hay datos listos para enviar
  if (dataReady) {
    sendDataToPC();
    
    // Esperar comando para nueva captura
    Serial.println("WAITING:COMMAND");
    while (!Serial.available()) {
      delay(100);
    }
    
    String command = Serial.readStringUntil('\n');
    command.trim();
    
    if (command == "START" || command == "CAPTURE") {
      startCapture();
    }
    return;
  }
  
  // Si está capturando
  if (isCapturing) {
    unsigned long currentTime = micros();
    unsigned long elapsedTime = currentTime - lastSampleTime;
    
    // Muestrear a 100 Hz
    if (elapsedTime >= SAMPLE_INTERVAL_US) {
      lastSampleTime = currentTime;
      
      // Leer timestamp
      Timestamps[currentSample] = millis();
      
      // Leer ECG
      DerivationI[currentSample] = MyBioBoard.AD8232_GetVoltage(AD8232_XS1);
      DerivationII[currentSample] = MyBioBoard.AD8232_GetVoltage(AD8232_XS2);
      DerivationIII[currentSample] = DerivationII[currentSample] - DerivationI[currentSample];
      
      // Leer acelerómetro
      sensors_event_t event;
      accel.getEvent(&event);
      AccelX[currentSample] = event.acceleration.x;
      AccelY[currentSample] = event.acceleration.y;
      AccelZ[currentSample] = event.acceleration.z;
      
      currentSample++;
      
      // Indicador de progreso cada 100 muestras (1 segundo)
      if (currentSample % 100 == 0) {
        Serial.print("PROGRESS:");
        Serial.print(currentSample);
        Serial.print("/");
        Serial.println(MAX_SAMPLES);
      }
      
      // Si se completó la captura
      if (currentSample >= MAX_SAMPLES) {
        isCapturing = false;
        dataReady = true;
        Serial.println("CAPTURE:COMPLETE");
      }
    }
  }
  
  yield();
}