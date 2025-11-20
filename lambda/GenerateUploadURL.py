"""
Lambda Function: GenerateUploadURL
Genera URLs presignadas de S3 y responde via MQTT a dispositivos IoT
Python 3.11 Runtime
"""

import json
import boto3
import os
from datetime import datetime

# Clientes AWS
s3_client = boto3.client('s3')
iot_client = boto3.client('iot-data')

# Configuración desde variables de entorno
BUCKET_NAME = os.environ.get('S3_BUCKET_NAME', 'holter-raw-data')
EXPIRATION_SECONDS = int(os.environ.get('URL_EXPIRATION', '3600'))  # 1 hora
REGION = os.environ.get('AWS_REGION', 'us-east-1')


def lambda_handler(event, context):
    """
    Handler principal de la Lambda
    
    Event esperado (via IoT Rule):
    {
        "device_id": "esp32-holter-001",
        "session_id": "session_1234567890",
        "timestamp": "1234567890",
        "file_size": 4500000,
        "ready_for_upload": true
    }
    """
    
    print(f"[INFO] Event recibido: {json.dumps(event)}")
    
    try:
        # Extraer datos del evento
        device_id = event.get('device_id')
        session_id = event.get('session_id')
        timestamp = event.get('timestamp')
        file_size = event.get('file_size', 0)
        
        # Validaciones
        if not device_id or not session_id:
            return error_response("device_id y session_id son requeridos")
        
        # Generar ruta S3: raw/YYYY/MM/DD/device_id/session_id.bin
        dt = datetime.fromtimestamp(int(timestamp))
        s3_key = f"raw/{dt.year}/{dt.month:02d}/{dt.day:02d}/{device_id}/{session_id}.bin"
        
        print(f"[INFO] Generando URL para: s3://{BUCKET_NAME}/{s3_key}")
        
        # Generar URL presignada PUT
        presigned_url = s3_client.generate_presigned_url(
            'put_object',
            Params={
                'Bucket': BUCKET_NAME,
                'Key': s3_key,
                'ContentType': 'application/octet-stream'
            },
            ExpiresIn=EXPIRATION_SECONDS,
            HttpMethod='PUT'
        )
        
        # Metadata para tracking
        metadata = {
            'device_id': device_id,
            'session_id': session_id,
            'timestamp': str(timestamp),
            'file_size': str(file_size),
            'generated_at': datetime.utcnow().isoformat(),
            's3_key': s3_key
        }
        
        print(f"[SUCCESS] URL generada (válida por {EXPIRATION_SECONDS}s)")
        
        # Preparar respuesta MQTT
        response_payload = {
            'status': 'success',
            'upload_url': presigned_url,
            's3_key': s3_key,
            'bucket': BUCKET_NAME,
            'expires_in': EXPIRATION_SECONDS,
            'timestamp': datetime.utcnow().isoformat()
        }
        
        # Publicar respuesta via MQTT
        response_topic = f"holter/upload-url/{device_id}"
        
        iot_client.publish(
            topic=response_topic,
            qos=1,
            payload=json.dumps(response_payload)
        )
        
        print(f"[MQTT] Respuesta enviada a topic: {response_topic}")
        
        # Guardar metadata en DynamoDB (opcional)
        save_metadata_to_dynamodb(metadata)
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'URL generada exitosamente',
                's3_key': s3_key
            })
        }
        
    except Exception as e:
        print(f"[ERROR] {str(e)}")
        return error_response(str(e))


def error_response(error_message):
    """Retorna respuesta de error"""
    return {
        'statusCode': 400,
        'body': json.dumps({
            'status': 'error',
            'message': error_message
        })
    }


def save_metadata_to_dynamodb(metadata):
    """
    Guarda metadata en DynamoDB para tracking (opcional)
    Tabla: holter-sessions
    """
    try:
        dynamodb = boto3.resource('dynamodb')
        table_name = os.environ.get('DYNAMODB_TABLE', 'holter-sessions')
        table = dynamodb.Table(table_name)
        
        item = {
            'device_id': metadata['device_id'],
            'session_id': metadata['session_id'],
            'timestamp': int(metadata['timestamp']),
            'file_size': int(metadata['file_size']),
            's3_key': metadata['s3_key'],
            'status': 'pending_upload',
            'generated_at': metadata['generated_at']
        }
        
        table.put_item(Item=item)
        print(f"[DynamoDB] Metadata guardada para {metadata['session_id']}")
        
    except Exception as e:
        print(f"[WARNING] No se pudo guardar en DynamoDB: {e}")
        # No fallar si DynamoDB no está disponible
        pass
