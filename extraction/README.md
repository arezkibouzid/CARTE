# Extraction documentaire des exercices — protocole 3.2

Un appel et un `rag_input.json` par exercice. Qwen3-VL-8B-Instruct annote les
fragments ; le code conserve textes, sources, quantités imprimées et coordonnées.
Aucune règle doctrinale ni calcul de route n'est ajouté.

## Installation et préparation

```bash
python3 -m venv .venv-extraction
source .venv-extraction/bin/activate
pip install -r extraction/requirements.txt
python extraction/prepare.py --source-dir /chemin/vers/pdf --out data/pages --dpi 150
python extraction/test_contract.py
```

Le corpus contient les SHA-256 attendus des trois PDF. Un autre PDF doit être
explicitement ajouté au corpus avec ses pages et son empreinte.

## Serveur GPU séparé

Version testée avant cette correction : vLLM 0.11.0, PyTorch 2.8.0+cu128,
Transformers 4.57.6, H100 80 Go. Installer vLLM dans un environnement séparé.

```bash
vllm serve /chemin/models/Qwen3-VL-8B-Instruct \
  --served-model-name Qwen/Qwen3-VL-8B-Instruct --host 127.0.0.1 --port 8001 \
  --dtype bfloat16 --max-model-len 32768 --max-num-seqs 1 \
  --gpu-memory-utilization 0.8 --limit-mm-per-prompt '{"image":8}' \
  --mm-processor-kwargs '{"max_pixels":2508800}' \
  --structured-outputs-config '{"backend":"xgrammar","disable_any_whitespace":true,"disable_fallback":true}' \
  --generation-config vllm --enforce-eager
```

## Un exercice

```bash
python extraction/run.py --case ex21_27_22B --pages-dir data/pages \
  --endpoint http://127.0.0.1:8001/v1/chat/completions --out results/22B_run01
```

Le dossier de sortie doit être nouveau. `--prepare-only` construit la requête sans
appeler le serveur. Tous les identifiants d'exercices figurent dans `corpus.json`.

## Interface RAG

- `rag_input.json` : enveloppe documentaire versionnée, pas des règles activées.
- `description` : texte principal complet en 3.2 ; peut alimenter un retrieval texte.
- `facts` : `id`, `text`, `page`, `quantities`, `annotation`.
- `evidence`, `printed_coordinates`, `issues`, `visual_notes` : provenance et incertitudes.
- `rag_query.txt` : description pour une API acceptant une chaîne.

L'exemple `examples/exercice_22B/rag_input.reviewed.json` utilise le contrat V2,
avec corrections de revue tracées dans `review_log.json`. Il correspond à l'exemple
V2 testé avec le RAG ; `rag_input.v2.original.json` est conservé intact.
**Les structures V2 et 3.2 diffèrent** : un consommateur de `facts` doit vérifier
`schema_version`. Ne pas présenter l'exemple revu comme une réponse brute 3.2.
En V2, utiliser les faits et leurs sources, la description seule étant un résumé.

## Corrections et limites

3.2 impose `category=output` pour `requested_output`. Une référence sur une page
avec image ne peut être déclarée absente sur le seul texte : provided ou unclear.
Le prompt explicite IN/OUT, les titres et les symboles de composition.
Le contrôle 22B exige la conservation de la référence de composition, et non une
absence présupposée. Aucun corrigé n'est envoyé au modèle.

15 tests locaux passent : schéma, provenance, nombres/heures, conservation des
fragments, export streaming, portée et régressions sémantiques connues.
**3.2 n'a pas encore été relancée sur GPU.** Les tests du client utilisent une
réponse simulée ; ils ne démontrent pas l'exactitude du modèle sur de nouveaux PDF.
Les essais GPU 3.1 sur 1, 8, 18, 22B terminaient sans troncature, mais avec erreurs
sémantiques connues. Les 28 exercices ne sont pas tous validés.
XGrammar garantit le format, pas la compréhension. Les symboles, tableaux et
ambiguïtés doivent être revus avant compilation vers le planner.

Ne versionner ni poids, ni environnements, ni PDF ou pages rendues, ni requêtes
contenant des images base64. Les résultats historiques restent inchangés.
