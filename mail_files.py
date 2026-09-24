"""Atomic, private attachment exports shared by native and local-cache readers."""
import hashlib
import os
from pathlib import Path
import tempfile


class ExportError(Exception):
    pass


def atomic_export(destination: str,producer,overwrite: bool=False) -> dict:
    requested=Path(destination).expanduser().absolute()
    target=requested.parent.resolve()/requested.name
    if not target.parent.is_dir() or target.is_dir():
        raise ExportError('Destination must be a file path in an existing directory.')
    if not overwrite and (target.exists() or target.is_symlink()):
        raise ExportError('Destination already exists; choose a new filename or set overwrite=true.')
    try:
        with tempfile.TemporaryDirectory(prefix='.mailmcp-',dir=target.parent) as directory:
            staged=Path(directory)/'attachment'
            metadata=producer(staged)
            if not staged.is_file() or staged.is_symlink():
                raise ExportError('Mail did not create the attachment file; no destination was written.')
            data=staged.read_bytes()
            os.chmod(staged,0o600)
            if overwrite:os.replace(staged,target)
            else:os.link(staged,target)
        return {'path':str(target),'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'attachment':metadata}
    except OSError as error:
        raise ExportError('Cannot export attachment: '+str(error)) from error
