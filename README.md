# ECG-IMU XSpace - IoT Holter System with AWS

Portable ESP32-based Holter monitoring system that captures 3-lead ECG signals and IMU accelerometer data, storing them in binary format and automatically uploading to AWS S3 via AWS IoT Core.

## 📋 Features

- **ECG Capture**: 3 leads (I, II, III) at 100 Hz
- **IMU Sensor**: ADXL345 Accelerometer (3-axis) at 100 Hz
- **Local Storage**: SD Card with optimized binary format (int16)
- **IoT Connectivity**: AWS IoT Core via MQTT over TLS
- **Automatic Upload**: S3 presigned URLs via Lambda
- **Test Mode**: Hardware-free testing to validate AWS communication
- **Low Power**: WiFi disabled during capture

## 🔧 Hardware

- **Board**: XSpace Bio V1.0 (ESP32)
- **ECG**: 2x AD8232 (leads I and II, III calculated)
- **IMU**: ADXL345 (I2C, optional)
- **Storage**: MicroSD Card (SPI, optional for testing)
- **WiFi**: 2.4 GHz integrated in ESP32

### Connections

```
ESP32 Pin 5  → SD Card CS
I2C SDA/SCL  → ADXL345
AD8232_XS1   → Lead I
AD8232_XS2   → Lead II
```

## 📦 Dependencies

```ini
[env:esp32dev]
platform = espressif32
board = esp32dev
framework = arduino

lib_deps = 
    adafruit/Adafruit ADXL345@^1.3.4
    knolleary/PubSubClient@^2.8
    bblanchon/ArduinoJson@^6.21.5
```

## 🏗️ AWS Infrastructure

### Architecture Overview

```
┌─────────────┐
│   ESP32     │
│   Device    │
└──────┬──────┘
       │ MQTT/TLS
       ↓
┌─────────────────────────────────────────────────────────┐
│                    AWS IoT Core                         │
│  ┌──────────────┐         ┌─────────────────────────┐  │
│  │  IoT Thing   │────────→│  IoT Rule               │  │
│  │  Certificate │         │  (SQL: SELECT * FROM    │  │
│  └──────────────┘         │   'holter/upload-req')  │  │
│                           └───────────┬─────────────┘  │
└───────────────────────────────────────┼────────────────┘
                                        │ Trigger
                                        ↓
                            ┌───────────────────────┐
                            │   Lambda Function 1   │
                            │  GenerateUploadURL    │
                            │  - Generate S3 URL    │
                            │  - Publish MQTT reply │
                            └───────────┬───────────┘
                                        │
                    ┌───────────────────┴────────────────┐
                    ↓                                    ↓
        ┌───────────────────────┐          ┌────────────────────┐
        │    S3 Bucket          │          │  IoT Core (MQTT)   │
        │  holter-raw-data      │          │  Response Topic    │
        │  /raw/device/file.bin │          │  holter/upload-url │
        └───────────┬───────────┘          └────────────────────┘
                    │ S3 Event                        ↓
                    │ Trigger                    (ESP32 receives)
                    ↓
        ┌───────────────────────┐
        │   Lambda Function 2   │
        │   ProcessECGData      │
        │   - Parse binary      │
        │   - Extract features  │
        │   - Store processed   │
        └───────────┬───────────┘
                    ↓
        ┌───────────────────────┐
        │    S3 Bucket          │
        │ holter-processed-data │
        │ /processed/metadata   │
        └───────────────────────┘
```

### Data Flow

1. **ESP32** → Captures ECG/IMU data → Stores locally on SD card
2. **ESP32** → Connects WiFi → Publishes to `holter/upload-request` (MQTT)
3. **IoT Rule** → Triggers **Lambda 1** (GenerateUploadURL)
4. **Lambda 1** → Generates S3 presigned URL → Publishes to `holter/upload-url/{device_id}`
5. **ESP32** → Receives URL → Uploads binary file to **S3 (raw-data)**
6. **S3 Event** → Triggers **Lambda 2** (ProcessECGData)
7. **Lambda 2** → Processes binary → Extracts features → Saves to **S3 (processed-data)**

## 📝 AWS Configuration

### 1. AWS IoT Core

Create a **Thing** in AWS IoT Core:

```bash
# Download certificates:
- Root CA (Amazon Root CA 1)
- Device Certificate
- Private Key
```

### 2. Configure `include/aws_config.h`

```cpp
#define WIFI_SSID "YourWiFi"
#define WIFI_PASSWORD "YourPassword"

#define AWS_IOT_ENDPOINT "xxxxxx-ats.iot.us-east-1.amazonaws.com"
#define DEVICE_ID "esp32-holter-001"

#define TOPIC_REQUEST "holter/upload-request"
#define TOPIC_RESPONSE "holter/upload-url/esp32-holter-001"

// Paste downloaded certificates
const char AWS_CERT_CA[] PROGMEM = R"EOF(
-----BEGIN CERTIFICATE-----
...
-----END CERTIFICATE-----
)EOF";

const char AWS_CERT_CRT[] PROGMEM = R"EOF(
-----BEGIN CERTIFICATE-----
...
-----END CERTIFICATE-----
)EOF";

const char AWS_CERT_PRIVATE[] PROGMEM = R"EOF(
-----BEGIN RSA PRIVATE KEY-----
...
-----END RSA PRIVATE KEY-----
)EOF";
```

### 3. IoT Rule

Create rule in AWS IoT Core to trigger Lambda:

```json
{
  "sql": "SELECT * FROM 'holter/upload-request'",
  "actions": [{
    "lambda": {
      "functionArn": "arn:aws:lambda:REGION:ACCOUNT:function:GenerateUploadURL"
    }
  }]
}
```

### 4. Lambda Function 1 - GenerateUploadURL

This Lambda must:
1. Receive message from ESP32 with metadata
2. Generate S3 presigned URL (PUT)
3. Publish response via MQTT to topic `holter/upload-url/{device_id}`

```python
import boto3
import json

s3_client = boto3.client('s3')
iot_client = boto3.client('iot-data')

def lambda_handler(event, context):
    device_id = event['device_id']
    session_id = event['session_id']
    
    # Generate presigned URL
    url = s3_client.generate_presigned_url(
        'put_object',
        Params={
            'Bucket': 'holter-raw-data',
            'Key': f'raw/{device_id}/{session_id}.bin',
            'ContentType': 'application/octet-stream'
        },
        ExpiresIn=3600
    )
    
    # Respond via MQTT
    iot_client.publish(
        topic=f'holter/upload-url/{device_id}',
        qos=1,
        payload=json.dumps({
            'status': 'success',
            'upload_url': url
        })
    )
    
    return {'statusCode': 200}
```

### 5. Lambda Function 2 - ProcessECGData

This Lambda is triggered by S3 events on the `holter-raw-data` bucket:

```python
import boto3
import struct
import json

s3_client = boto3.client('s3')

def lambda_handler(event, context):
    # Get bucket and file info from S3 event
    bucket = event['Records'][0]['s3']['bucket']['name']
    key = event['Records'][0]['s3']['object']['key']
    
    # Download binary file
    response = s3_client.get_object(Bucket=bucket, Key=key)
    data = response['Body'].read()
    
    # Parse header (32 bytes)
    header_format = '<IHHIIHHII4x'  # little-endian format
    header = struct.unpack(header_format, data[:32])
    
    magic, version, device_id, session_id, timestamp_start, \
    ecg_sample_rate, imu_sample_rate, num_ecg_samples, num_imu_samples = header
    
    # Parse ECG samples (6 bytes each: 3x int16)
    ecg_samples = []
    offset = 32
    for i in range(num_ecg_samples):
        lead_i, lead_ii, lead_iii = struct.unpack('<hhh', data[offset:offset+6])
        ecg_samples.append({
            'lead_i': lead_i / 6553.6,  # Convert to mV
            'lead_ii': lead_ii / 6553.6,
            'lead_iii': lead_iii / 6553.6
        })
        offset += 6
        
        # Skip IMU data (12 bytes)
        offset += 12
    
    # Process and extract features
    processed_data = {
        'device_id': device_id,
        'session_id': session_id,
        'timestamp': timestamp_start,
        'sample_rate': ecg_sample_rate,
        'num_samples': num_ecg_samples,
        'ecg_data': ecg_samples,
        # Add your processing here (QRS detection, HR calculation, etc.)
    }
    
    # Save processed data to processed-data bucket
    output_key = f"processed/{device_id}/{session_id}.json"
    s3_client.put_object(
        Bucket='holter-processed-data',
        Key=output_key,
        Body=json.dumps(processed_data),
        ContentType='application/json'
    )
    
    return {'statusCode': 200}
```

### 6. S3 Buckets Configuration

Create two S3 buckets:

**Bucket 1: holter-raw-data**
```json
{
  "NotificationConfiguration": {
    "LambdaFunctionConfigurations": [{
      "LambdaFunctionArn": "arn:aws:lambda:REGION:ACCOUNT:function:ProcessECGData",
      "Events": ["s3:ObjectCreated:*"],
      "Filter": {
        "Key": {
          "FilterRules": [{
            "Name": "prefix",
            "Value": "raw/"
          }]
        }
      }
    }]
  }
}
```

**Bucket 2: holter-processed-data**
- Standard configuration
- Used to store processed JSON files

### 7. IAM Permissions

**Lambda 1 (GenerateUploadURL) Execution Role:**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::holter-raw-data/*"
    },
    {
      "Effect": "Allow",
      "Action": "iot:Publish",
      "Resource": "arn:aws:iot:REGION:ACCOUNT:topic/holter/upload-url/*"
    }
  ]
}
```

**Lambda 2 (ProcessECGData) Execution Role:**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject"
      ],
      "Resource": "arn:aws:s3:::holter-raw-data/*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:PutObject"
      ],
      "Resource": "arn:aws:s3:::holter-processed-data/*"
    }
  ]
}
```

**S3 Bucket Policy (holter-raw-data):**

Grant Lambda permission to be triggered by S3 events:

```bash
aws lambda add-permission \
  --function-name ProcessECGData \
  --statement-id S3InvokeLambda \
  --action lambda:InvokeFunction \
  --principal s3.amazonaws.com \
  --source-arn arn:aws:s3:::holter-raw-data
```

## 🚀 Usage

### Compile and Upload

```bash
platformio run -t upload
platformio device monitor
```

### Operation Flow

1. **Startup**: ESP32 boots and verifies hardware
2. **Capture**: 
   - With SD: Captures 10 seconds of ECG/IMU
   - Without SD: Test mode (AWS communication only)
3. **WiFi Connection**: Connects after capture
4. **MQTT**: 
   - Connects to AWS IoT Core
   - Publishes request to `holter/upload-request` topic
5. **Lambda 1**: Generates presigned URL
6. **Upload**: 
   - With SD: Uploads binary file to S3 (raw-data)
   - Without SD: Completes test flow
7. **Processing**: S3 event triggers Lambda 2
8. **Lambda 2**: Processes binary and saves to S3 (processed-data)
9. **Restart**: Cycle repeats every 10 seconds

### Duration Configuration

Modify in `src/main.cpp`:

```cpp
const int CAPTURE_DURATION_SEC = 10;  // Change to 30, 60, 1800, etc.
```

## 📊 Data Format

### Binary File (`.bin`)

```
[Header 32 bytes]
[ECG Sample 1: 6 bytes]
[IMU Sample 1: 12 bytes]
[ECG Sample 2: 6 bytes]
[IMU Sample 2: 12 bytes]
...
```

#### Header (32 bytes)

```c
struct FileHeader {
  uint32_t magic;              // 0x45434744 = "ECGD"
  uint16_t version;            // 1
  uint16_t device_id;          // Device ID
  uint32_t session_id;         // Unix timestamp
  uint32_t timestamp_start;    // Unix timestamp start
  uint16_t ecg_sample_rate;    // 100 Hz
  uint16_t imu_sample_rate;    // 100 Hz
  uint32_t num_ecg_samples;    // Total ECG samples
  uint32_t num_imu_samples;    // Total IMU samples
  uint8_t reserved[4];         // Reserved
} __attribute__((packed));
```

#### ECG Sample (6 bytes)

```c
struct ECGSample {
  int16_t lead_i;      // Scaled: ±5mV → ±32768
  int16_t lead_ii;     // Factor: 6553.6
  int16_t lead_iii;    // III = II - I
} __attribute__((packed));
```

#### IMU Sample (12 bytes)

```c
struct IMUSample {
  int16_t accel_x;  // Acceleration X
  int16_t accel_y;  // Acceleration Y
  int16_t accel_z;  // Acceleration Z
  int16_t gyro_x;   // Gyroscope X (not implemented)
  int16_t gyro_y;   // Gyroscope Y (not implemented)
  int16_t gyro_z;   // Gyroscope Z (not implemented)
} __attribute__((packed));
```

### Voltage Conversion

```python
# Convert int16 to mV
voltage_mV = int16_value / 6553.6
```

### File Size

For 10-second capture:
- ECG: 1000 samples × 6 bytes = 6 KB
- IMU: 1000 samples × 12 bytes = 12 KB
- Header: 32 bytes
- **Total**: ~18 KB

For 30 minutes (1800s):
- **Total**: ~3.2 MB

## 🔍 Debugging

### System Logs

```
[OK]      - Successful operation
[INFO]    - General information
[WARNING] - Warning (non-critical)
[ERROR]   - Error (can continue)
[DEBUG]   - Debug information
```

### Common Errors

#### 1. MQTT Connection Lost (-3)

```
[DEBUG] MQTT State: -3
[DEBUG] MQTT Connected: NO
```

**Solution**: 
- Verify certificates in `aws_config.h`
- Verify AWS IoT endpoint
- Verify IoT policy (iot:Connect, iot:Publish, iot:Subscribe)

#### 2. Timeout Waiting for URL

```
[ERROR] Timeout waiting for URL (60s)
```

**Solution**:
- Verify IoT Rule is enabled
- Verify Lambda has `iot:Publish` permissions
- Check Lambda logs in CloudWatch

#### 3. Failed to Publish

```
[ERROR] Failed to publish
```

**Solution**:
- Increase `mqttClient.setBufferSize(4096)`
- Verify topic matches IoT Rule
- Verify keepAlive and call `mqttClient.loop()`

#### 4. Lambda 2 Processing Errors

```
[ERROR] Failed to process binary file
```

**Solution**:
- Verify S3 event notification is configured
- Check Lambda 2 has GetObject permission on raw-data bucket
- Check Lambda 2 has PutObject permission on processed-data bucket
- Review Lambda logs in CloudWatch

## 📈 AWS Monitoring

### MQTT Test Client

In AWS IoT Console → Test:

```
# Subscribe to see ESP32 messages
holter/upload-request

# Subscribe to see Lambda responses
holter/upload-url/#
```

### CloudWatch Logs

```
/aws/lambda/GenerateUploadURL
/aws/lambda/ProcessECGData
```

### S3 Buckets

**Raw Data Structure:**
```
holter-raw-data/
└── raw/
    └── esp32-holter-001/
        └── 1234567890.bin
```

**Processed Data Structure:**
```
holter-processed-data/
└── processed/
    └── esp32-holter-001/
        └── 1234567890.json
```

## 🛠️ Development

### Test Mode (Without Hardware)

The system automatically detects missing hardware:

- **Without IMU**: Uses 0 values for accelerometer
- **Without SD**: Skips capture, only tests AWS communication

Useful for:
- AWS connectivity testing
- Development without complete hardware
- Lambda integration validation

### Configurable Parameters

```cpp
// Capture duration
const int CAPTURE_DURATION_SEC = 10;

// Sampling frequencies
const int ECG_SAMPLE_RATE_HZ = 100;
const int IMU_SAMPLE_RATE_HZ = 100;

// ECG scaling (mV → int16)
const float ECG_SCALE_FACTOR = 6553.6;

// SD write buffer
const int BUFFER_SIZE = 512;

// Upload timeout
const unsigned long UPLOAD_TIMEOUT_MS = 30000;
```

## 📋 TODO / Future Improvements

- [ ] GZIP compression of files before upload
- [ ] Real-time QRS detection
- [ ] Low power mode (deep sleep between captures)
- [ ] NTP synchronization for precise timestamps
- [ ] OTA (Over-The-Air) updates via AWS
- [ ] Web dashboard for real-time visualization
- [ ] DynamoDB storage for metadata
- [ ] Lambda processing for feature extraction
- [ ] Heart rate variability (HRV) analysis
- [ ] Anomaly detection alerts
- [ ] Multi-device dashboard

## 📄 License

This project was developed for the Instrumentation course at PUCP.

## 👥 Author

Leon Achata

## 🔗 References

- [ESP32 Arduino Core](https://github.com/espressif/arduino-esp32)
- [AWS IoT Core Documentation](https://docs.aws.amazon.com/iot/)
- [PubSubClient MQTT Library](https://github.com/knolleary/pubsubclient)
- [XSpace Bio Board](https://github.com/XSpaceTech)
- [AWS Lambda Documentation](https://docs.aws.amazon.com/lambda/)
- [AWS S3 Event Notifications](https://docs.aws.amazon.com/AmazonS3/latest/userguide/NotificationHowTo.html)
