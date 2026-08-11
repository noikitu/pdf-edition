"""Assistance par LLM : le modèle *propose* des corrections, il ne touche jamais au PDF.

Le document n'est jamais envoyé au fournisseur : seuls les fragments de texte le
sont, accompagnés d'un identifiant. Le modèle renvoie une liste de remplacements
que l'interface affiche pour validation ; l'application passe ensuite par la
route d'édition ordinaire, donc par la pile d'annulation.

LangChain n'est utilisé que pour une chose, mais elle compte : une interface
unique pour les trois fournisseurs et une sortie structurée vérifiée, sans écrire
un adaptateur par API.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.util import find_spec

from pydantic import BaseModel, Field

from . import pdf_ops

# Un lot par appel au modèle : assez grand pour qu'il voie le contexte d'un
# paragraphe, assez petit pour rester loin des limites de contexte et de coût.
BATCH_SIZE = 120
MAX_BATCH_CHARS = 24_000
# Garde-fou de coût : au-delà, on s'arrête et on le dit franchement à l'utilisateur.
MAX_BATCHES = 12


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    module: str          # paquet à installer pour l'activer
    default_model: str

    @property
    def available(self) -> bool:
        return find_spec(self.module) is not None


PROVIDERS = (
    Provider("anthropic", "Anthropic (Claude)", "langchain_anthropic", "claude-sonnet-5"),
    Provider("openai", "OpenAI (GPT)", "langchain_openai", "gpt-5.1"),
    Provider("gemini", "Google (Gemini)", "langchain_google_genai", "gemini-3-pro"),
)


def catalogue() -> list[dict]:
    return [
        {
            "key": p.key,
            "label": p.label,
            "default_model": p.default_model,
            "available": p.available,
            "module": p.module,
        }
        for p in PROVIDERS
    ]


def get_provider(key: str) -> Provider:
    for provider in PROVIDERS:
        if provider.key == key:
            return provider
    raise ValueError(f"Fournisseur inconnu : {key}")


# --------------------------------------------------------------------------
# Sortie attendue du modèle
# --------------------------------------------------------------------------

class Suggestion(BaseModel):
    id: str = Field(description="identifiant du fragment, repris exactement tel quel")
    text: str = Field(description="nouveau texte complet du fragment")
    reason: str = Field(default="", description="justification en une courte phrase, en français")


class Suggestions(BaseModel):
    edits: list[Suggestion] = Field(
        default_factory=list, description="uniquement les fragments à modifier"
    )


SYSTEM_PROMPT = """Tu corriges le texte d'un document PDF.

On te donne des fragments numérotés, chacun avec un identifiant. Tu renvoies la
liste des seuls fragments à modifier, avec leur nouveau texte complet.

Règles impératives :
- Reprends l'identifiant exactement tel qu'il t'est donné.
- Renvoie le texte complet du fragment, pas seulement la partie changée.
- Ne renvoie pas les fragments que tu ne modifies pas.
- Reste dans la langue du document.
- Pas de Markdown, pas de guillemets ajoutés, pas de saut de ligne : ces
  fragments sont écrits tels quels dans la page.
- Garde une longueur proche de l'original. Un fragment beaucoup plus long
  débordera sur le texte voisin.
- N'invente aucune information absente du document.
- Si rien n'est à corriger, renvoie une liste vide.

Un fragment est une portion de ligne : il peut commencer ou finir au milieu
d'une phrase, et la ponctuation manquante peut se trouver dans le fragment
suivant. Ne « répare » pas ce découpage."""


def _build_model(provider: Provider, api_key: str, model: str):
    """Instancie le modèle de chat du fournisseur choisi.

    Import tardif : l'application doit démarrer et fonctionner sans aucun de ces
    paquets, l'assistance étant facultative.
    """
    if not provider.available:
        raise RuntimeError(
            f"Le paquet {provider.module} n'est pas installé. "
            "Installez-le avec : pip install -r requirements-llm.txt"
        )
    # La température est volontairement laissée par défaut : plusieurs modèles
    # récents refusent qu'on la fixe.
    if provider.key == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=model, api_key=api_key, timeout=90, max_retries=1)
    if provider.key == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model, api_key=api_key, timeout=90, max_retries=1)
    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(model=model, google_api_key=api_key, max_retries=1)


def _batches(items: list[pdf_ops.TextItem]) -> list[list[pdf_ops.TextItem]]:
    """Découpe les fragments en lots, par nombre et par volume de texte."""
    out: list[list[pdf_ops.TextItem]] = []
    current: list[pdf_ops.TextItem] = []
    size = 0
    for item in items:
        if current and (len(current) >= BATCH_SIZE or size + len(item.text) > MAX_BATCH_CHARS):
            out.append(current)
            current, size = [], 0
        current.append(item)
        size += len(item.text)
    if current:
        out.append(current)
    return out


def _render(items: list[pdf_ops.TextItem]) -> str:
    return "\n".join(f"[{item.id}] {item.text}" for item in items)


def suggest(
    doc,
    pages: list[int],
    instruction: str,
    provider_key: str,
    api_key: str,
    model: str = "",
) -> dict:
    """Demande au modèle des corrections sur les pages indiquées.

    Renvoie {suggestions, examined, batches, truncated}. Rien n'est modifié ici :
    l'application des corrections retenues passe par la route d'édition.
    """
    provider = get_provider(provider_key)
    chat = _build_model(provider, api_key, model or provider.default_model)
    structured = chat.with_structured_output(Suggestions)

    items: list[pdf_ops.TextItem] = []
    for pno in pages:
        items.extend(pdf_ops.extract_items(doc, pno))
    index = {item.id: item for item in items}

    batches = _batches(items)
    truncated = len(batches) > MAX_BATCHES
    batches = batches[:MAX_BATCHES]

    suggestions: list[dict] = []
    seen: set[str] = set()
    for batch in batches:
        message = (
            f"Consigne de l'utilisateur : {instruction}\n\n"
            f"Fragments :\n{_render(batch)}"
        )
        result = structured.invoke(
            [("system", SYSTEM_PROMPT), ("human", message)]
        )
        for edit in getattr(result, "edits", []) or []:
            item = index.get(edit.id)
            # Un identifiant inventé, ou un fragment renvoyé inchangé, n'a rien
            # à faire dans la liste soumise à l'utilisateur.
            if item is None or edit.id in seen:
                continue
            text = " ".join(edit.text.split())
            if not text or text == item.text.strip():
                continue
            seen.add(edit.id)
            suggestions.append({
                "id": item.id,
                "page": item.page,
                "original": item.text,
                "text": text,
                "reason": (edit.reason or "").strip(),
            })

    return {
        "suggestions": suggestions,
        "examined": sum(len(b) for b in batches),
        "batches": len(batches),
        "truncated": truncated,
    }
