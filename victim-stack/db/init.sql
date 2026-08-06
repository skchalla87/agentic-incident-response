-- Trivial seed data. No migrations framework, on purpose.
CREATE TABLE widgets (
    id    SERIAL PRIMARY KEY,
    name  TEXT NOT NULL,
    price NUMERIC(10, 2) NOT NULL
);

INSERT INTO widgets (name, price)
SELECT 'widget-' || i, (i * 1.5)::numeric(10, 2)
FROM generate_series(1, 50) AS i;
