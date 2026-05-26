from pathlib import Path


def test_kb_maintenance_scripts_do_not_embed_production_pg_passwords():
    scripts = [
        Path("scripts/backfill_hadith_chapters.py"),
        Path("scripts/sync_kb_v2_to_pg.py"),
    ]
    production_password = "HejazDB" "2026Secure"

    for script in scripts:
        text = script.read_text()
        assert production_password not in text
        assert 'default="postgresql://' not in text
        assert 'os.getenv("PG_DSN", "postgresql://' not in text
