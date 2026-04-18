"""
Copias de seguridad de financiera.db (compatible con ejecución empaquetada).
"""
import os
import shutil
import sqlite3
import logging
from datetime import datetime

import db
from config import app_base_dir

logger = logging.getLogger(__name__)


def backups_dir() -> str:
    path = os.path.join(app_base_dir(), "backups")
    os.makedirs(path, exist_ok=True)
    return path


def _sidecar_paths(db_file: str) -> list[str]:
    paths = [db_file]
    for suf in ("-wal", "-shm"):
        p = db_file + suf
        if os.path.isfile(p):
            paths.append(p)
    return paths


def _db_backups() -> list[str]:
    folder = backups_dir()
    files = []
    for name in os.listdir(folder):
        if name.lower().endswith(".db") and name.startswith("financiera_backup_"):
            files.append(os.path.join(folder, name))
    files.sort(key=os.path.getmtime, reverse=True)
    return files


def _checkpoint_db(db_file: str) -> None:
    """
    Intenta consolidar WAL en el archivo principal.
    Si falla, no interrumpe el backup.
    """
    try:
        con = sqlite3.connect(db_file)
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            con.close()
    except sqlite3.Error as e:
        # El backup sigue siendo valido; no se detiene la operacion.
        logger.warning("Fallo wal_checkpoint para %s: %s", db_file, e)


def _validar_backup(db_file: str) -> None:
    """
    Verifica que el backup sea abrible y pase un integrity_check basico.
    """
    try:
        con = sqlite3.connect(db_file)
        try:
            row = con.execute("PRAGMA integrity_check").fetchone()
            if not row or row[0] != "ok":
                raise sqlite3.DatabaseError("integrity_check no fue OK")
        finally:
            con.close()
    except sqlite3.Error as e:
        logger.exception("Backup invalido para %s", db_file)
        raise RuntimeError(f"El backup generado no paso validacion: {e}") from e


def _copiar_backup(src: str, dst: str, incluir_sidecars: bool = True) -> None:
    """
    Copia el .db y, opcionalmente, sus sidecars WAL/SHM.
    """
    shutil.copy2(src, dst)
    if incluir_sidecars:
        for suf in ("-wal", "-shm"):
            s = src + suf
            if os.path.isfile(s):
                shutil.copy2(s, dst + suf)


def hacer_backup() -> str:
    """Copia la base actual a backups/financiera_backup_YYYY-MM-DD_HH-MM-SS.db"""
    src = db.db_path()
    if not os.path.isfile(src):
        raise FileNotFoundError("No existe financiera.db. Abre la app al menos una vez para crearla.")

    out_dir = backups_dir()
    name = f"financiera_backup_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.db"
    dst = os.path.join(out_dir, name)

    _checkpoint_db(src)
    _copiar_backup(src, dst, incluir_sidecars=True)
    _validar_backup(dst)

    return dst


def hacer_backup_automatico(marca: str = "auto") -> str:
    """
    Crea una copia automática con nombre:
    financiera_backup_auto_YYYY-MM-DD_HH-MM-SS.db
    """
    src = db.db_path()
    if not os.path.isfile(src):
        raise FileNotFoundError("No existe financiera.db. Abre la app al menos una vez para crearla.")

    out_dir = backups_dir()
    name = f"financiera_backup_{marca}_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.db"
    dst = os.path.join(out_dir, name)

    _checkpoint_db(src)
    _copiar_backup(src, dst, incluir_sidecars=True)
    _validar_backup(dst)
    return dst


def existe_backup_automatico_hoy() -> bool:
    hoy = datetime.now().strftime("%Y-%m-%d")
    for path in _db_backups():
        if os.path.basename(path).startswith(f"financiera_backup_auto_{hoy}_"):
            return True
    return False


def limpiar_backups(max_archivos: int = 30) -> int:
    """
    Conserva solo los backups .db más recientes (manuales + automáticos).
    Retorna cuántos archivos fueron eliminados.
    """
    if max_archivos < 1:
        max_archivos = 1
    files = _db_backups()
    removed = 0
    for path in files[max_archivos:]:
        try:
            os.remove(path)
            for suf in ("-wal", "-shm"):
                side = path + suf
                if os.path.isfile(side):
                    os.remove(side)
            removed += 1
        except OSError as e:
            logger.warning("No se pudo eliminar backup antiguo %s: %s", path, e)
            continue
    return removed


def restaurar_desde(archivo_backup: str) -> None:
    """Sobrescribe financiera.db con el archivo seleccionado."""
    archivo_backup = os.path.abspath(archivo_backup)
    if not os.path.isfile(archivo_backup):
        raise FileNotFoundError("El archivo de respaldo no existe.")

    if not archivo_backup.lower().endswith(".db"):
        raise ValueError("Selecciona un archivo .db")

    target = db.db_path()

    try:
        con = sqlite3.connect(target)
        con.close()
    except sqlite3.Error as e:
        logger.warning("No se pudo abrir la base objetivo %s: %s", target, e)

    for p in _sidecar_paths(target):
        if os.path.isfile(p):
            try:
                os.remove(p)
            except OSError as e:
                raise OSError(f"No se pudo liberar la base actual: {e}") from e

    shutil.copy2(archivo_backup, target)
    for suf in ("-wal", "-shm"):
        sb = archivo_backup + suf
        td = target + suf
        if os.path.isfile(sb):
            shutil.copy2(sb, td)
        elif os.path.isfile(td):
            try:
                os.remove(td)
            except OSError as e:
                logger.warning("No se pudo eliminar sidecar obsoleto %s: %s", td, e)
