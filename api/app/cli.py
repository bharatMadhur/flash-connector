import argparse

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models import Tenant



def create_tenant(
    tenant_name: str,
    parent_tenant_id: str | None = None,
    can_create_subtenants: bool = True,
) -> None:
    db = SessionLocal()
    try:
        tenant = db.scalar(select(Tenant).where(Tenant.name == tenant_name))
        if tenant is not None:
            print(f"Tenant already exists: {tenant_name} (tenant_id={tenant.id})")
            return

        tenant = Tenant(
            name=tenant_name,
            parent_tenant_id=parent_tenant_id,
            can_create_subtenants=can_create_subtenants,
        )
        db.add(tenant)
        db.commit()
        db.refresh(tenant)
        print(f"Created tenant {tenant_name} (tenant_id={tenant.id})")
    finally:
        db.close()



def main() -> None:
    parser = argparse.ArgumentParser(description="flash-connector CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_tenant_parser = subparsers.add_parser("create-tenant", help="Create a tenant/workspace")
    create_tenant_parser.add_argument("--tenant", required=True, help="Tenant name")
    create_tenant_parser.add_argument("--parent-tenant-id", default=None, help="Optional parent tenant id")
    create_tenant_parser.add_argument(
        "--can-create-subtenants",
        default="true",
        choices=["true", "false"],
        help="Whether tenant may create subtenants",
    )

    args = parser.parse_args()

    if args.command == "create-tenant":
        create_tenant(
            tenant_name=args.tenant,
            parent_tenant_id=args.parent_tenant_id,
            can_create_subtenants=args.can_create_subtenants == "true",
        )


if __name__ == "__main__":
    main()
