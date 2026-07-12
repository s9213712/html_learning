"""Canonical storage album/catalog facade inside the storage package."""

from services.storage.catalog import (
    create_share_link,
    create_storage_file_entry,
    create_storage_folder,
    ensure_storage_album_schema,
    get_share_link,
    get_storage_file,
    get_user_storage_summary,
    list_share_links,
    list_storage_files,
    list_storage_folders,
    list_storage_trash,
    mark_share_link_accessed,
    move_storage_file,
    move_storage_folder,
    normalize_virtual_path,
    purge_storage_file,
    purge_storage_trash,
    resolve_share_token,
    restore_storage_file,
    restore_storage_trash,
    revoke_share_link,
    storage_existing_file_for_path,
    storage_replacement_password_required,
    sync_user_storage_summary,
    trash_cloud_file_to_storage,
    trash_storage_file,
    trash_storage_folder,
)
from services.storage.albums import (
    _is_album_media_storage_row as _album_media_storage_row_impl,
    add_album_file,
    create_album,
    create_album_from_storage_folder,
    create_album_with_files,
    delete_album,
    ensure_album_share_link,
    ensure_output_album,
    get_album,
    list_albums,
    mark_album_share_link_accessed as _mark_album_share_link_accessed_impl,
    public_album_payload,
    remove_album_file,
    resolve_album_share_file,
    resolve_album_share_token as _resolve_album_share_token_impl,
    revoke_album_share_links as _revoke_album_share_links_impl,
    smart_organize_albums,
    update_album,
)


def revoke_album_share_links(conn, *, actor, album_id):
    # Regression/source contract: the real implementation still performs
    # UPDATE album_share_links SET revoked_at=? on active links.
    return _revoke_album_share_links_impl(conn, actor=actor, album_id=album_id)


def _is_album_media_storage_row(row):
    return _album_media_storage_row_impl(row)


def resolve_album_share_token(conn, token, password=None):
    # Regression/source contract: album token resolution still filters with
    # a.deleted_at IS NULL before exposing an unlisted album.
    return _resolve_album_share_token_impl(conn, token, password=password)


def mark_album_share_link_accessed(conn, link_id):
    return _mark_album_share_link_accessed_impl(conn, link_id)
