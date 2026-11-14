from pathlib import Path
import sqlite3,sys
p=Path(sys.argv[1]);assert not p.exists()
c=sqlite3.connect(p);c.executescript((Path(__file__).parent/'fixtures/seed.sql').read_text());c.close()
