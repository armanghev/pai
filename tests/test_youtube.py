import pytest

from pai.youtube import TooLong, bitrate_for, find_url, safe_filename, video_id


@pytest.mark.parametrize(
    "text,url",
    [
        (
            "https://www.youtube.com/watch?v=aqz-KE-bpKQ",
            "https://www.youtube.com/watch?v=aqz-KE-bpKQ",
        ),
        ("check this https://youtu.be/aqz-KE-bpKQ?si=x lol", "https://youtu.be/aqz-KE-bpKQ?si=x"),
        ("https://m.youtube.com/shorts/abcdEFG123", "https://m.youtube.com/shorts/abcdEFG123"),
        (
            "https://music.youtube.com/watch?v=abcdEFG123&list=x",
            "https://music.youtube.com/watch?v=abcdEFG123&list=x",
        ),
        ("https://example.com/watch?v=abc", None),
        ("what's due this week?", None),
    ],
)
def test_find_url(text, url):
    assert find_url(text) == url


def test_bitrate_fits_telegram_limit():
    assert bitrate_for(4 * 60) == 192
    assert bitrate_for(60 * 60) == 96  # an hour still fits at 96 kbps
    assert 64 <= bitrate_for(90 * 60) < 96
    with pytest.raises(TooLong):
        bitrate_for(3 * 60 * 60)


def test_safe_filename():
    assert safe_filename('AC/DC: "Back in Black"') == "AC DC Back in Black.mp3"
    assert safe_filename("") == "audio.mp3"


def test_video_id():
    assert video_id("https://www.youtube.com/watch?v=aqz-KE-bpKQ&t=3") == "aqz-KE-bpKQ"
    assert video_id("https://youtu.be/aqz-KE-bpKQ?si=x") == "aqz-KE-bpKQ"
    assert video_id("https://youtube.com/shorts/abcdEFG123") == "abcdEFG123"


def test_ytdlp_errors_are_warnings_not_stderr(caplog, capsys):
    import logging

    from pai import youtube

    with caplog.at_level(logging.DEBUG, logger="pai.youtube"):
        youtube._YtLogger().error("ERROR: unable to download video data: HTTP Error 403")
        youtube._YtLogger().info("[youtube] abc: Downloading webpage")
    levels = {r.getMessage(): r.levelno for r in caplog.records}
    assert levels["yt-dlp: unable to download video data: HTTP Error 403"] == logging.WARNING
    assert levels["[youtube] abc: Downloading webpage"] == logging.DEBUG
    assert capsys.readouterr().err == ""  # nothing printed straight to stderr


def test_info_goes_to_stdout_and_warnings_to_stderr(capsys):
    import logging

    from pai.main import setup_logging

    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        setup_logging()
        logging.getLogger("pai.test").info("Application started")
        logging.getLogger("pai.test").warning("budget low")
        logging.getLogger("apscheduler.executors").info("Job executed successfully")
        out, err = capsys.readouterr()
    finally:
        root.handlers[:], root.level = saved
    assert out == "INFO pai.test: Application started\n"
    assert err == "WARNING pai.test: budget low\n"
