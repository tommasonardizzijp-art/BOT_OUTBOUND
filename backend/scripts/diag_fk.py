import asyncio, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from sqlalchemy import text
from app.database import AsyncSessionLocal

# Elenca tutti i FK che puntano a campaigns.id e la loro delete_rule (CASCADE/NO ACTION/...)
SQL = """
select tc.table_name  as child_table,
       kcu.column_name as child_col,
       rc.delete_rule
from information_schema.referential_constraints rc
join information_schema.table_constraints tc
     on tc.constraint_name = rc.constraint_name and tc.constraint_schema = rc.constraint_schema
join information_schema.key_column_usage kcu
     on kcu.constraint_name = rc.constraint_name and kcu.constraint_schema = rc.constraint_schema
join information_schema.constraint_column_usage ccu
     on ccu.constraint_name = rc.constraint_name and ccu.constraint_schema = rc.constraint_schema
where ccu.table_name = 'campaigns' and ccu.column_name = 'id'
order by tc.table_name;
"""


async def main():
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text(SQL))).all()
        if not rows:
            print("Nessun FK trovato verso campaigns.id (o DB non-postgres).")
            return
        print("FK -> campaigns.id  |  delete_rule:")
        for child_table, child_col, rule in rows:
            flag = "" if rule == "CASCADE" else "   <-- BLOCCA la delete"
            print(f"  {child_table}.{child_col:16} {rule}{flag}")


asyncio.run(main())
