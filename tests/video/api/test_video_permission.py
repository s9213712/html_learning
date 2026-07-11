import pytest

from services.media.videos import can_view_video, get_video, list_videos, publish_video
from tests.video.helpers.video_test_helpers import actor, seed_cloud_file, video_test_db


def _published(conn, visibility):
    seed_cloud_file(conn, file_id=f"file-{visibility}", owner_user_id=1, mime="video/mp4")
    return publish_video(
        conn,
        actor=actor(1, "owner"),
        cloud_file_id=f"file-{visibility}",
        title=f"{visibility} video",
        visibility=visibility,
    )


def test_video_visibility_rules():
    conn = video_test_db()
    public = _published(conn, "public")
    unlisted = _published(conn, "unlisted")
    private = _published(conn, "private")

    assert get_video(conn, public["id"], actor=None)["visibility"] == "public"
    with pytest.raises(PermissionError):
        get_video(conn, unlisted["id"], actor=None)
    assert get_video(conn, unlisted["id"], actor=actor(1, "owner"))["visibility"] == "unlisted"
    with pytest.raises(PermissionError):
        get_video(conn, unlisted["id"], actor=actor(3, "manager", "manager"))

    with pytest.raises(PermissionError):
        get_video(conn, private["id"], actor=actor(2, "viewer"))

    assert get_video(conn, private["id"], actor=actor(1, "owner"))["id"] == private["id"]
    with pytest.raises(PermissionError):
        get_video(conn, private["id"], actor=actor(3, "manager", "manager"))


def test_unlisted_video_is_link_accessible_but_not_publicly_listed():
    conn = video_test_db()
    public = _published(conn, "public")
    unlisted = _published(conn, "unlisted")

    with pytest.raises(PermissionError):
        get_video(conn, unlisted["id"], actor=None)
    anonymous_ids = {row["id"] for row in list_videos(conn, actor=None)}
    viewer_ids = {row["id"] for row in list_videos(conn, actor=actor(2, "viewer"))}
    owner_ids = {row["id"] for row in list_videos(conn, actor=actor(1, "owner"))}
    manager_ids = {row["id"] for row in list_videos(conn, actor=actor(3, "manager", "manager"))}

    assert public["id"] in anonymous_ids
    assert unlisted["id"] not in anonymous_ids
    assert unlisted["id"] not in viewer_ids
    assert unlisted["id"] in owner_ids
    assert unlisted["id"] not in manager_ids


def test_blocked_video_metadata_owner_visible_but_stream_denied():
    conn = video_test_db()
    video = _published(conn, "public")
    conn.execute("UPDATE videos SET status='blocked' WHERE id=?", (video["id"],))
    row = conn.execute("SELECT * FROM videos WHERE id=?", (video["id"],)).fetchone()
    assert can_view_video(actor(1, "owner"), row)
    assert not can_view_video(actor(1, "owner"), row, for_stream=True)


def test_video_payload_allows_server_decrypted_range_stream_but_not_e2ee():
    conn = video_test_db()
    seed_cloud_file(
        conn,
        file_id="server-encrypted-video",
        owner_user_id=1,
        mime="video/mp4",
        privacy_mode="server_encrypted",
    )
    encrypted = publish_video(
        conn,
        actor=actor(1, "owner"),
        cloud_file_id="server-encrypted-video",
        title="Encrypted video",
        visibility="public",
    )
    seed_cloud_file(conn, file_id="plain-video", owner_user_id=1, mime="video/mp4")
    plain = publish_video(
        conn,
        actor=actor(1, "owner"),
        cloud_file_id="plain-video",
        title="Plain video",
        visibility="public",
    )
    seed_cloud_file(
        conn,
        file_id="e2ee-video",
        owner_user_id=1,
        mime="video/mp4",
        privacy_mode="e2ee",
    )
    e2ee = publish_video(
        conn,
        actor=actor(1, "owner"),
        cloud_file_id="e2ee-video",
        title="E2EE video",
        visibility="private",
    )
    seed_cloud_file(conn, file_id="proxy-only-video", owner_user_id=1, mime="video/mp4")
    proxy_only = publish_video(
        conn,
        actor=actor(1, "owner"),
        cloud_file_id="proxy-only-video",
        title="Realtime proxy video",
        visibility="public",
        streaming_modes=["realtime_proxy"],
    )

    encrypted_payload = get_video(conn, encrypted["id"], actor=actor(2, "viewer"))
    plain_payload = get_video(conn, plain["id"], actor=actor(2, "viewer"))
    e2ee_payload = get_video(conn, e2ee["id"], actor=actor(1, "owner"))
    proxy_only_payload = get_video(conn, proxy_only["id"], actor=actor(2, "viewer"))

    assert encrypted_payload["cloud_privacy_mode"] == "server_encrypted"
    assert encrypted_payload["direct_stream_allowed"] is True
    assert plain_payload["direct_stream_allowed"] is True
    assert e2ee_payload["cloud_privacy_mode"] == "e2ee"
    assert e2ee_payload["direct_stream_allowed"] is False
    assert proxy_only_payload["streaming_modes"] == ["realtime_proxy"]
    assert proxy_only_payload["direct_stream_allowed"] is False
