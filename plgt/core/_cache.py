import logging
import shutil
from pathlib import Path

from . import settings

logger = logging.getLogger(__name__)


class DependencyCache:
    """Global cache handler for managing cached files and manifests."""

    dir: Path

    def __init__(self):
        self.dir = settings.PROJECT_DIR / "deps"
        self.dir.mkdir(parents=True, exist_ok=True)

    def exists(self, path: Path) -> bool:
        """Check if a cache entry exists."""
        return (self.dir / path).exists()

    def read(self, path: Path) -> bytes:
        """Read object from cache."""
        cache_asset_path = self.dir / path

        return cache_asset_path.open("rb").read()

    def write(self, path: Path, content: bytes) -> Path:
        """Write object to the local cache."""

        cache_asset_path = self.dir / path
        cache_asset_path.parent.mkdir(parents=True, exist_ok=True)

        with cache_asset_path.open("wb") as f:
            f.write(content)

        return cache_asset_path

    def remove(self, path: Path) -> Path:
        """Remove a cache entry."""
        cache_asset_path = self.dir / path

        if not cache_asset_path.exists():
            logger.warning("Cache entry %s does not exist.", cache_asset_path)
            return

        if cache_asset_path.is_dir():
            parent = cache_asset_path.parent

            shutil.rmtree(cache_asset_path)

            if len(list(parent.iterdir())) == 0:
                parent.rmdir()

            return

        cache_asset_path.unlink()
