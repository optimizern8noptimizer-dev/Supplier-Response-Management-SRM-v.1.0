"""
ERP Маркет API Client
Синхронизация претензий из ERP-системы.
Адаптировать под реальные endpoint'ы после получения документации API.
"""
import httpx
import logging
from typing import Optional
from config import settings

logger = logging.getLogger(__name__)

ERP_HEADERS = {
    "Authorization": f"Bearer {settings.ERP_API_TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}


class ErpApiClient:

    def __init__(self):
        self.base_url = settings.ERP_API_URL.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {settings.ERP_API_TOKEN}",
            "Content-Type": "application/json",
        }

    async def get_claim(self, claim_number: str) -> Optional[dict]:
        """
        Получить данные одной претензии из ERP по номеру.
        Возвращает нормализованный dict или None если не найдена.

        Предполагаемый endpoint: GET /claims/{claim_number}
        АДАПТИРОВАТЬ под реальный API Маркет.
        """
        if not self.base_url:
            logger.warning("[ERP] ERP_API_URL не настроен, возвращаю mock данные")
            return self._mock_claim(claim_number)

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"{self.base_url}/claims/{claim_number}",
                    headers=self.headers
                )
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                raw = response.json()
                return self._normalize_claim(raw)
        except httpx.HTTPError as e:
            logger.error(f"[ERP] HTTP ошибка при запросе претензии {claim_number}: {e}")
            raise

    async def get_claims_batch(
        self,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> list[dict]:
        """
        Получить список претензий из ERP для синхронизации.
        Предполагаемый endpoint: GET /claims?status=...&limit=...&offset=...
        АДАПТИРОВАТЬ под реальный API Маркет.
        """
        if not self.base_url:
            return []

        params = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.get(
                    f"{self.base_url}/claims",
                    headers=self.headers,
                    params=params
                )
                response.raise_for_status()
                data = response.json()
                # Предполагаем { "items": [...], "total": N }
                items = data.get("items", data) if isinstance(data, dict) else data
                return [self._normalize_claim(item) for item in items]
        except httpx.HTTPError as e:
            logger.error(f"[ERP] HTTP ошибка при пакетном запросе: {e}")
            return []

    def _normalize_claim(self, raw: dict) -> dict:
        """
        Нормализует структуру ответа ERP API в единый формат системы.
        АДАПТИРОВАТЬ под реальную структуру JSON из Маркет API.

        Ожидаемый формат на выходе:
        {
            "claim_number": "103827",
            "order_number": "97486778",
            "supplier_name": "ООО Поставщик",
            "supplier_inn": "123456789",
            "planned_delivery_date": "2026-05-06",
            "actual_delivery_date": "2026-05-07",
            "total_penalty_amount": 5076.65,
            "currency": "BYN",
            "items": [ {...}, ... ]
        }
        """
        # ─── АДАПТАЦИЯ НИЖЕ ─────────────────────────────────────────────────
        # Замените ключи под реальную структуру JSON из ERP Маркет
        items = []
        for item in raw.get("items", raw.get("positions", raw.get("lines", []))):
            items.append({
                "sku":                item.get("sku") or item.get("article") or item.get("good_code"),
                "product_name":       item.get("product_name") or item.get("good_name") or item.get("name"),
                "ordered_qty":        float(item.get("ordered_qty") or item.get("ordered") or 0),
                "received_qty":       float(item.get("received_qty") or item.get("received") or 0),
                "promo_qty":          float(item.get("promo_qty") or item.get("promo") or 0),
                "allowed_deviation_pct": float(item.get("allowed_deviation_pct") or item.get("deviation_pct") or 0),
                "shortage_qty":       float(item.get("shortage_qty") or item.get("shortage") or 0),
                "penalized_qty":      float(item.get("penalized_qty") or item.get("penalty_qty") or 0),
                "unit_price":         float(item.get("unit_price") or item.get("price") or 0),
                "penalty_rate_pct":   float(item.get("penalty_rate_pct") or item.get("penalty_rate") or 0),
                "penalty_amount":     float(item.get("penalty_amount") or item.get("fine") or 0),
            })

        return {
            "claim_number":         str(raw.get("claim_number") or raw.get("id") or raw.get("claim_id", "")),
            "order_number":         str(raw.get("order_number") or raw.get("order_id", "")),
            "supplier_name":        raw.get("supplier_name") or raw.get("supplier", {}).get("name", ""),
            "supplier_inn":         raw.get("supplier_inn") or raw.get("supplier", {}).get("inn", ""),
            "planned_delivery_date": raw.get("planned_delivery_date") or raw.get("plan_date", ""),
            "actual_delivery_date": raw.get("actual_delivery_date") or raw.get("fact_date", ""),
            "total_penalty_amount": float(raw.get("total_penalty_amount") or raw.get("total_fine") or 0),
            "currency":             raw.get("currency", "BYN"),
            "items":                items,
        }

    def _mock_claim(self, claim_number: str) -> dict:
        """
        Mock-данные для разработки и тестирования без ERP.
        Соответствует примеру из PDF Претензия №103827.
        """
        return {
            "claim_number": claim_number,
            "order_number": "97486778",
            "supplier_name": "ООО Тестовый Поставщик",
            "supplier_inn": "123456789",
            "planned_delivery_date": "2026-05-06",
            "actual_delivery_date": "2026-05-07",
            "total_penalty_amount": 5076.65,
            "currency": "BYN",
            "items": [
                {
                    "sku": "494623",
                    "product_name": 'Мойва"ЖИРНАЯ"(х/к) 1кг Баренцево',
                    "ordered_qty": 124.0, "received_qty": 123.0,
                    "promo_qty": 0.0, "allowed_deviation_pct": 0.0,
                    "shortage_qty": 1.0, "penalized_qty": 1.0,
                    "unit_price": 25.76, "penalty_rate_pct": 20.0, "penalty_amount": 5.15,
                },
                {
                    "sku": "549906",
                    "product_name": 'Скумбрия"БАРСКАЯ"(атл,кус,х/к)Баренц300г',
                    "ordered_qty": 5280.0, "received_qty": 2500.0,
                    "promo_qty": 0.0, "allowed_deviation_pct": 0.0,
                    "shortage_qty": 2780.0, "penalized_qty": 2780.0,
                    "unit_price": 9.07, "penalty_rate_pct": 20.0, "penalty_amount": 5044.03,
                },
            ],
        }


# Singleton клиент
erp_client = ErpApiClient()
