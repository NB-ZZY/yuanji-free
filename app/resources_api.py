"""Local curriculum import endpoints; protected by the application's local API guard."""
from fastapi import APIRouter, File, HTTPException, UploadFile
import resource_pack

router = APIRouter()


@router.get('/api/resources')
def resource_status():
    return resource_pack.status()


@router.post('/api/resources/import')
def import_resources(file: UploadFile = File(...)):
    try:
        return resource_pack.import_zip(file.file.read(resource_pack.MAX_ARCHIVE + 1))
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        file.file.close()
