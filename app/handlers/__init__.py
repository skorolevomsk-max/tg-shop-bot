"""Handler routers."""

from aiogram import Router

from app.handlers import admin, cart, catalog, checkout, common


def build_main_router() -> Router:
    router = Router(name="main")
    router.include_router(common.router)
    router.include_router(catalog.router)
    router.include_router(cart.router)
    router.include_router(checkout.router)
    router.include_router(admin.router)
    return router
