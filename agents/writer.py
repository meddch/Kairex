from openai import OpenAI

SYSTEM_PROMPT = """Tu es un assistant spécialisé dans la synthèse de connaissances organisationnelles.
On te fournit une liste de faits structurés approuvés sur une organisation.
Ton rôle est de produire un bloc de contexte en français, prêt à être injecté dans un prompt LLM.

Le bloc doit :
- Être rédigé en français clair et professionnel
- Synthétiser les faits de manière cohérente et fluide (pas une simple liste)
- Commencer par "## Contexte organisationnel"
- Grouper les informations par entité lorsque c'est pertinent
- Être concis mais complet — chaque fait approuvé doit apparaître

Ne retourne que le bloc de contexte. Pas d'introduction ni d'explication."""


class WriterAgent:
    def __init__(self, api_key: str):
        self.client = OpenAI(api_key=api_key)

    def generate_context(self, org_id: str, facts: list[dict]) -> str:
        facts_text = "\n".join(
            f"- [{f['entity_type']}] {f['entity_value']} | {f['attribute']}: {f['value']} "
            f"(confiance: {f['confidence']:.0%})"
            for f in facts
        )

        response = self.client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Organisation ID: {org_id}\n\n"
                        f"Faits approuvés:\n{facts_text}"
                    ),
                },
            ],
            temperature=0.3,
        )

        return response.choices[0].message.content.strip()
