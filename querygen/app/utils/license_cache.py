"""
License Key Caching Service

This module implements a robust caching strategy for license key validation
to prevent rate limiting from the Polar API.

Key Features:
- 6-hour TTL (Time To Live) for cached validations
- Atomic operations to prevent race conditions
- Graceful degradation (uses cache if Polar fails)
- Single-request refresh protection
"""

from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Any
from sqlmodel import Session, select
from polar_sdk import Polar
import logging
import os
from dotenv import load_dotenv
from app.models.models import LicenseCache

load_dotenv()

POLAR_ACCESS_TOKEN = os.getenv("POLAR_ACCESS_TOKEN") or ""

# Default TTL: 6 hours (recommended for license validation)
DEFAULT_CACHE_TTL_HOURS = 6

logger = logging.getLogger(__name__)


def serialize_datetime(obj: Any) -> Any:
    """
    Recursively serialize datetime objects to ISO format strings.

    This is needed because JSONB fields cannot store datetime objects directly.

    Args:
        obj: Any object that may contain datetime objects

    Returns:
        The same object with all datetime objects converted to ISO strings
    """
    if isinstance(obj, datetime):
        return obj.isoformat()
    elif isinstance(obj, dict):
        return {key: serialize_datetime(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [serialize_datetime(item) for item in obj]
    else:
        return obj


class LicenseCacheService:
    """Service for caching license key validation results."""

    def __init__(self, ttl_hours: int = DEFAULT_CACHE_TTL_HOURS):
        """
        Initialize the license cache service.

        Args:
            ttl_hours: Time to live for cache entries in hours (default: 6)
        """
        self.ttl_hours = ttl_hours
        self.polar = Polar(
            access_token=POLAR_ACCESS_TOKEN,
            server="sandbox" if os.getenv("ENVIRONMENT") == "DEV" else None,
        )
        self.organization_id = (
            "0dc53c95-b0e7-4961-85d5-5731b1f471fe"
            if os.getenv("ENVIRONMENT") == "DEV"
            else "04c23754-7621-46c1-9b53-4cb4da2da349"
        )

    def _is_cache_valid(self, cache_entry: LicenseCache) -> bool:
        """
        Check if a cache entry is still valid based on TTL.

        Args:
            cache_entry: The cache entry to check

        Returns:
            True if cache is still valid, False if expired
        """
        if cache_entry.expires_at is None:
            return False

        now = datetime.now(timezone.utc)

        # Ensure expires_at is timezone-aware
        expires_at = cache_entry.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        return expires_at > now

    async def validate_license_key(
        self, session: Session, license_key: str, organization_id: Optional[str] = None
    ) -> Dict:
        """
        Validate a license key with intelligent caching.

        This method implements the following flow:
        1. Check DB for cached result
        2. If cache is valid and not expired, return it
        3. If cache is expired or doesn't exist, validate with Polar
        4. If Polar fails (429, timeout, etc.), fall back to cached result
        5. Update cache with new result

        Args:
            session: SQLModel database session
            license_key: The license key to validate
            organization_id: Optional organization ID (uses default if not provided)

        Returns:
            Dict with validation result and metadata
        """
        org_id = organization_id or self.organization_id

        # Step 1: Check cache
        statement = select(LicenseCache).where(LicenseCache.license_key == license_key)
        cache_entry = session.exec(statement).first()

        # Step 2: Return valid cache
        if cache_entry and self._is_cache_valid(cache_entry):
            logger.info(f"Cache HIT for license key: {license_key[:8]}...")
            return {
                "validated": cache_entry.status.get("validated", False),
                "from_cache": True,
                "cached_at": cache_entry.checked_at.isoformat(),
                "expires_at": (
                    cache_entry.expires_at.isoformat()
                    if cache_entry.expires_at
                    else None
                ),
                **cache_entry.status,
            }

        # Step 3: Cache miss or expired - validate with Polar
        logger.info(
            f"Cache MISS for license key: {license_key[:8]}... - Validating with Polar"
        )

        # Check if another request is already refreshing
        if cache_entry and cache_entry.refresh_in_progress:
            logger.info(
                f"Refresh in progress for {license_key[:8]}... - Using cached value"
            )
            return {
                "validated": cache_entry.status.get("validated", False),
                "from_cache": True,
                "refresh_in_progress": True,
                **cache_entry.status,
            }

        # Mark refresh as in progress (atomic operation protection)
        if cache_entry:
            cache_entry.refresh_in_progress = True
            session.add(cache_entry)
            session.commit()

        try:
            # Call Polar API
            response = await self.polar.customer_portal.license_keys.validate_async(
                request={
                    "key": license_key,
                    "organization_id": org_id,
                }
            )

            # Successful validation
            now = datetime.now(timezone.utc)
            expires_at = now + timedelta(hours=self.ttl_hours)

            # Serialize the Polar response to ensure JSON compatibility
            polar_response = (
                response.model_dump() if hasattr(response, "model_dump") else {}
            )
            polar_response_serialized = serialize_datetime(polar_response)

            status_data = {
                "validated": True,
                "polar_response": polar_response_serialized,
                "last_validation_success": True,
            }

            # Update or create cache entry
            if cache_entry:
                cache_entry.status = status_data
                cache_entry.checked_at = now
                cache_entry.expires_at = expires_at
                cache_entry.refresh_in_progress = False
                session.add(cache_entry)
            else:
                cache_entry = LicenseCache(
                    license_key=license_key,
                    status=status_data,
                    checked_at=now,
                    expires_at=expires_at,
                    refresh_in_progress=False,
                )
                session.add(cache_entry)

            session.commit()
            session.refresh(cache_entry)

            logger.info(
                f"Successfully validated and cached license key: {license_key[:8]}..."
            )

            return {
                "validated": True,
                "from_cache": False,
                "cached_at": cache_entry.checked_at.isoformat(),
                "expires_at": (
                    cache_entry.expires_at.isoformat()
                    if cache_entry.expires_at
                    else None
                ),
                **status_data,
            }

        except Exception as e:
            logger.error(f"Polar API error for {license_key[:8]}...: {str(e)}")

            # Step 4: Graceful degradation - use cached result if available
            if cache_entry:
                cache_entry.refresh_in_progress = False
                session.add(cache_entry)
                session.commit()

                logger.warning(
                    f"Falling back to cached result for {license_key[:8]}... "
                    f"due to Polar error: {str(e)}"
                )

                return {
                    "validated": cache_entry.status.get("validated", False),
                    "from_cache": True,
                    "cache_fallback": True,
                    "polar_error": str(e),
                    "cached_at": cache_entry.checked_at.isoformat(),
                    **cache_entry.status,
                }

            # No cache available - validation failed
            now = datetime.now(timezone.utc)
            expires_at = now + timedelta(minutes=5)  # Short TTL for failures

            status_data = {
                "validated": False,
                "error": str(e),
                "last_validation_success": False,
            }

            cache_entry = LicenseCache(
                license_key=license_key,
                status=status_data,
                checked_at=now,
                expires_at=expires_at,
                refresh_in_progress=False,
            )
            session.add(cache_entry)
            session.commit()

            return {
                "validated": False,
                "from_cache": False,
                "error": str(e),
                **status_data,
            }

    def invalidate_cache(self, session: Session, license_key: str) -> bool:
        """
        Manually invalidate a cached license key.

        Useful for:
        - Forcing immediate revalidation
        - Handling license revocations
        - Testing

        Args:
            session: SQLModel database session
            license_key: The license key to invalidate

        Returns:
            True if cache was invalidated, False if not found
        """
        statement = select(LicenseCache).where(LicenseCache.license_key == license_key)
        cache_entry = session.exec(statement).first()

        if cache_entry:
            session.delete(cache_entry)
            session.commit()
            logger.info(f"Invalidated cache for license key: {license_key[:8]}...")
            return True

        logger.info(f"No cache found to invalidate for: {license_key[:8]}...")
        return False

    def get_cache_stats(self, session: Session) -> Dict:
        """
        Get statistics about the license cache.

        Args:
            session: SQLModel database session

        Returns:
            Dict with cache statistics
        """
        statement = select(LicenseCache)
        all_entries = session.exec(statement).all()

        now = datetime.now(timezone.utc)
        valid_count = sum(1 for e in all_entries if self._is_cache_valid(e))
        expired_count = len(all_entries) - valid_count

        return {
            "total_cached_keys": len(all_entries),
            "valid_entries": valid_count,
            "expired_entries": expired_count,
            "ttl_hours": self.ttl_hours,
        }


# Singleton instance
_license_cache_service: Optional[LicenseCacheService] = None


def get_license_cache_service() -> LicenseCacheService:
    """
    Get the singleton instance of LicenseCacheService.

    Returns:
        LicenseCacheService instance
    """
    global _license_cache_service
    if _license_cache_service is None:
        _license_cache_service = LicenseCacheService()
    return _license_cache_service
