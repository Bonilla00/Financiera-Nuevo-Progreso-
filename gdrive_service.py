"""
Servicio de integración con Google Drive OAuth 2.0 por usuario y gestión de carpetas y evidencias.
"""
from __future__ import annotations

import os
import json
import logging
from base64 import urlsafe_b64encode
from hashlib import sha256
from cryptography.fernet import Fernet

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import io

import db

logger = logging.getLogger(__name__)

# Configurar entorno de OAuth fuera de localhost para producción en Railway si es necesario
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'


def _get_fernet() -> Fernet:
    secret = os.environ.get("SECRET_KEY", "financiera-nuevo-progreso-secret-key")
    key = urlsafe_b64encode(sha256(secret.encode()).digest())
    return Fernet(key)


def encriptar_token(token_json: str) -> str:
    f = _get_fernet()
    return f.encrypt(token_json.encode()).decode()


def descifrar_token(token_encriptado: str) -> str:
    f = _get_fernet()
    return f.decrypt(token_encriptado.encode()).decode()


def obtener_credenciales_google(user_id: int) -> Credentials | None:
    row = db.obtener_google_token(user_id)
    if not row or not row.get("token_data"):
        return None
    try:
        token_json_str = descifrar_token(row["token_data"])
        t_info = json.loads(token_json_str)
        client_id = (os.environ.get("GOOGLE_CLIENT_ID") or "").strip().strip('"').strip("'")
        client_secret = (os.environ.get("GOOGLE_CLIENT_SECRET") or "").strip().strip('"').strip("'")
        
        creds = Credentials(
            token=t_info.get("token"),
            refresh_token=t_info.get("refresh_token"),
            token_uri=t_info.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=client_id or t_info.get("client_id"),
            client_secret=client_secret or t_info.get("client_secret"),
            scopes=t_info.get("scopes", ["https://www.googleapis.com/auth/drive.file"])
        )
        return creds
    except Exception as e:
        logger.error(f"Error descifrando token de usuario {user_id}: {e}")
        return None


def crear_flow_oauth(redirect_uri: str) -> Flow:
    client_id = (os.environ.get("GOOGLE_CLIENT_ID") or "").strip().strip('"').strip("'")
    client_secret = (os.environ.get("GOOGLE_CLIENT_SECRET") or "").strip().strip('"').strip("'")
    
    client_config = {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri]
        }
    }
    
    flow = Flow.from_client_config(
        client_config,
        scopes=["https://www.googleapis.com/auth/drive.file", "https://www.googleapis.com/auth/userinfo.email"],
        redirect_uri=redirect_uri
    )
    return flow


def obtener_servicio_drive(user_id: int):
    creds = obtener_credenciales_google(user_id)
    if not creds:
        return None
    return build('drive', 'v3', credentials=creds)


def _buscar_o_crear_carpeta(service, nombre: str, parent_id: str | None = None) -> str:
    query = f"name='{nombre}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    else:
        query += " and 'root' in parents"
        
    results = service.files().list(q=query, spaces='drive', fields="files(id, name)").execute()
    files = results.get('files', [])
    
    if files:
        return files[0]['id']
    
    file_metadata = {
        'name': nombre,
        'mimeType': 'application/vnd.google-apps.folder'
    }
    if parent_id:
        file_metadata['parents'] = [parent_id]
        
    folder = service.files().create(body=file_metadata, fields='id').execute()
    return folder.get('id')


def obtener_carpeta_credito_en_drive(user_id: int, cliente_id: int, cliente_nombre: str, cliente_identificacion: str, prestamo_id: int) -> str | None:
    service = obtener_servicio_drive(user_id)
    if not service:
        return None
        
    try:
        # 1. Carpeta raíz: FINANCIERA NUEVO PROGRESO
        root_id = _buscar_o_crear_carpeta(service, "FINANCIERA NUEVO PROGRESO")
        
        # 2. Carpeta CLIENTES
        clientes_id = _buscar_o_crear_carpeta(service, "CLIENTES", root_id)
        
        # 3. Carpeta del cliente
        cliente_folder_name = f"{cliente_id} - {cliente_nombre.upper()} ({cliente_identificacion})"
        cliente_folder_id = _buscar_o_crear_carpeta(service, cliente_folder_name, clientes_id)
        
        # Guardar en DB referencia de carpeta de cliente
        db.guardar_cliente_drive_folder(cliente_id, cliente_folder_id)
        
        # 4. Carpeta CREDITOS dentro del cliente
        creditos_id = _buscar_o_crear_carpeta(service, "CREDITOS", cliente_folder_id)
        
        # 5. Carpeta del préstamo específico
        prestamo_folder_name = f"CREDITO #{prestamo_id}"
        prestamo_folder_id = _buscar_o_crear_carpeta(service, prestamo_folder_name, creditos_id)
        
        return prestamo_folder_id
    except Exception as e:
        logger.error(f"Error creando carpetas en Drive para préstamo {prestamo_id}: {e}")
        return None


def subir_archivo_a_drive(user_id: int, folder_id: str, file_stream: io.BytesIO, file_name: str, mime_type: str) -> dict | None:
    service = obtener_servicio_drive(user_id)
    if not service:
        return None
        
    try:
        file_metadata = {
            'name': file_name,
            'parents': [folder_id]
        }
        media = MediaIoBaseUpload(file_stream, mimetype=mime_type, resumable=True)
        
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id, name, mimeType, size'
        ).execute()
        
        return {
            "file_id": file.get('id'),
            "file_name": file.get('name'),
            "mime_type": file.get('mimeType'),
            "size_bytes": int(file.get('size', 0))
        }
    except Exception as e:
        logger.error(f"Error subiendo archivo a Drive: {e}")
        return None


def eliminar_archivo_de_drive(user_id: int, file_id: str) -> bool:
    service = obtener_servicio_drive(user_id)
    if not service:
        return False
    try:
        service.files().delete(fileId=file_id).execute()
        return True
    except Exception as e:
        logger.error(f"Error eliminando archivo {file_id} de Drive: {e}")
        return False
