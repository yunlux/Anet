# Relationship activity follows observer-local append order

Incremental relationship activity uses a cursor over each observer's immutable
event append order, not a sort over reported occurrence time. Platform and
offline evidence may arrive late with an older timestamp; ordering by that
timestamp would place new records behind an already consumed cursor and make
them invisible, while append order gives stable replay at the cost of showing
occurrence time and persistence order as distinct concepts.
