from dataclasses import dataclass


class LimitExceeded(ValueError):
    pass


@dataclass
class CloudLimits:
    max_ocr_pages: int | None = None
    max_llm_calls: int | None = None
    ocr_pages: int = 0
    llm_calls: int = 0

    def reserve_ocr(self, pages: int) -> None:
        if pages < 1:
            raise ValueError("Nombre de pages Azure requis")
        if self.max_ocr_pages is not None and self.ocr_pages + pages > self.max_ocr_pages:
            raise LimitExceeded("Limite de pages Azure atteinte : arrêt avant le prochain appel")
        self.ocr_pages += pages

    def reserve_llm(self) -> None:
        if self.max_llm_calls is not None and self.llm_calls + 1 > self.max_llm_calls:
            raise LimitExceeded("Limite d'appels IA atteinte : arrêt avant le prochain appel")
        self.llm_calls += 1
