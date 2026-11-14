CREATE TABLE public.records(id int PRIMARY KEY, body text, qty int); CREATE PUBLICATION author_pub FOR TABLE public.records;
SELECT slot_name FROM pg_create_logical_replication_slot('author_slot','pgoutput');
INSERT INTO records VALUES(1,(SELECT string_agg(md5(g::text),'') FROM generate_series(1,128) g),10),(2,'small',20);
UPDATE records SET qty=11 WHERE id=1; UPDATE records SET id=3 WHERE id=2;
ALTER TABLE records ADD COLUMN note text; UPDATE records SET note='added',qty=12 WHERE id=1;
DELETE FROM records WHERE id=3;
