# Descriptif du sujet de hackathon AMIAD – CoE IA

Source: `20250929_NP_AMIAD_Description-sujet-hackathon.pdf` (organisers' brief), transcribed verbatim.

## Contexte

La France porte depuis mi 2026 un projet de centre d'excellence international pour l'IA de défense à Rennes. L'une des missions de ce futur centre d'excellence est la stimulation de l'écosystème international pour l'IA de défense qu'il s'agisse d'organisations étatiques, de laboratoires de recherche ou d'industriels de défense.

Pour concrétiser la collaboration internationale en matière d'innovation en IA de défense, l'AMIAD et l'équipe de préfiguration du centre d'excellence organise un hackathon qui se déroulera en amont de l'European Cyber Week 2026 du 12 au 14 Novembre. Ce hackathon est organisé avec le soutien de l'école du train et de la logistique afin d'avoir un sujet répondant à des problématiques au plus proche des besoins opérationnels.

1. **Sujet** : Développer une solution permettant de déterminer de manière automatique et évolutive un réseau routier de manœuvre dans un contexte de transport logistique militaire. Ce réseau doit pouvoir être automatiquement adapté en fonction d'informations reçues sur des incidents qui surviendraient au cours de la manœuvre.

2. **Contexte** : Les LLM permettent désormais de déterminer des itinéraires en fonction de requêtes courtes en langage naturel [1,2]. Néanmoins, les fenêtres de contexte grandissantes et les capacités des LLMs devenant de plus en plus vastes (multimodalité, frameworks multi-agents), la question se pose sur la possibilité de prendre en compte des éléments de contexte supplémentaires pour assister les opérationnels dans leur décision sur les voies à emprunter afin d'atteindre l'objectif.

3. **Hypothèse** : Les capacités multimodales des modèles et les fenêtres de contexte extrêmement larges (plusieurs centaines de milliers de tokens) pourraient permettre de prendre en compte, en plus de la carte, des éléments de doctrine, la constitution des équipages logistiques (types de véhicules etc.) ainsi que les contraintes temporelles et potentiellement les incidents en cours de route afin de choisir le ou les meilleures voies pour déplacer l'ensemble des convois.

4. **Les données d'entrée** :
   - Une carte IGN avec BD TOPO (format open street map) et/ou directement en image vectorisée ;
   - Des documents de doctrine OTAN ;
   - Un ordre logistique élaboré par les militaires de l'école du train ;
   - Des incidents en cours d'exercice.

5. **Défis techniques** :
   - Intégrer efficacement le plus d'éléments possibles de doctrines OTAN en plus de la carte et de l'objectif afin d'avoir un réseau routier de manœuvre le plus en phase avec les objectifs opérationnels. Possibilité de soit tout traiter avec un LLM multimodal pour du Visual Question Answering avec la carte, ou bien d'extraire l'information des éléments de contexte avec des techniques de NLP et les traduire en contraintes pour un algorithme de recherche de chemin ;
   - Optimiser la représentation des éléments de doctrines extraits et des contraintes opérationnelles pour ne pas surcharger la fenêtre de contexte ;
   - Les opérationnels n'ayant pas la capacité d'embarquer un datacenter dans leur convoi, essayer d'atteindre cet objectif avec des modèles de taille la plus modeste possible (quelques dizaines de milliards de paramètres au plus) ;

## Références

1. Bao et Yonetani, 2025, *Path Planning using Instruction-Guided Probabilistic Roadmaps*
2. Song et al., 2026, *MobilityBench: A Benchmark for Evaluating Route-Planning Agents in Real-World Mobility Scenarios*
3. Koukouraki et al., 2026, *Introducing the VISQAM Dataset: Toward Automated Map Interpretation* (Zenodo)
4. Ung et al., 2025, *CartoMapQA: A Fundamental Benchmark Dataset Evaluating Vision-Language Models on Cartographic Map Understanding*
5. Pyo et al., 2026, *FRIEDA: Benchmarking Multi-Step Cartographic Reasoning in Vision-Language Models*
