import asyncio
from typing import List, Union
import logging
import instructor
from openai import OpenAI
from cognee.modules.cognify.graph.add_data_chunks import add_data_chunks
from cognee.modules.cognify.graph.add_document_node import add_document_node
from cognee.modules.cognify.graph.add_classification_nodes import add_classification_nodes
from cognee.modules.cognify.graph.add_cognitive_layer_graphs import add_cognitive_layer_graphs
from cognee.modules.cognify.graph.add_summary_nodes import add_summary_nodes
from cognee.modules.cognify.graph.add_node_connections import group_nodes_by_layer, \
    graph_ready_output, connect_nodes_in_graph
from cognee.modules.cognify.llm.resolve_cross_graph_references import resolve_cross_graph_references

from cognee.config import Config

from cognee.infrastructure.databases.graph.get_graph_client import get_graph_client
from cognee.modules.cognify.graph.add_label_nodes import add_label_nodes
from cognee.modules.cognify.graph.add_cognitive_layers import add_cognitive_layers
# from cognee.modules.cognify.graph.initialize_graph import initialize_graph
from cognee.infrastructure.files.utils.guess_file_type import guess_file_type, FileTypeException
from cognee.infrastructure.files.utils.extract_text_from_file import extract_text_from_file
from cognee.infrastructure import infrastructure_config
from cognee.modules.data.get_content_categories import get_content_categories
from cognee.modules.data.get_content_summary import get_content_summary
from cognee.modules.data.get_cognitive_layers import get_cognitive_layers
from cognee.modules.data.get_layer_graphs import get_layer_graphs

config = Config()
config.load()

aclient = instructor.patch(OpenAI())

USER_ID = "default_user"

logger = logging.getLogger(__name__)

async def cognify(datasets: Union[str, List[str]] = None):
    """This function is responsible for the cognitive processing of the content."""

    db_engine = infrastructure_config.get_config()["database_engine"]

    if datasets is None or len(datasets) == 0:
        datasets = db_engine.get_datasets()

    awaitables = []

    # datasets is a list of dataset names
    if isinstance(datasets, list):
        for dataset in datasets:
            awaitables.append(cognify(dataset))

        graphs = await asyncio.gather(*awaitables)
        return graphs[0]

    # datasets is a dataset name string
    added_datasets = db_engine.get_datasets()

    dataset_files = []
    dataset_name = datasets.replace(".", "_").replace(" ", "_")

    for added_dataset in added_datasets:
        if dataset_name in added_dataset:
            dataset_files.append((added_dataset, db_engine.get_files_metadata(added_dataset)))

    awaitables = []

    graph_db_type = infrastructure_config.get_config()["graph_engine"]

    graph_client = await get_graph_client(graph_db_type)

    # await initialize_graph(USER_ID, graph_data_model, graph_client)

    data_chunks = {}

    for (dataset_name, files) in dataset_files:
        for file_metadata in files[:3]:
            with open(file_metadata["file_path"], "rb") as file:
                try:
                    file_type = guess_file_type(file)
                    text = extract_text_from_file(file, file_type)

                    if dataset_name not in data_chunks:
                        data_chunks[dataset_name] = []

                    data_chunks[dataset_name].append(dict(text = text, file_metadata = file_metadata))
                except FileTypeException:
                    logger.warning("File (%s) has an unknown file type. We are skipping it.", file_metadata["id"])

    added_chunks: list[tuple[str, str, dict]] = await add_data_chunks(data_chunks)

    await asyncio.gather(
        *[process_text(chunk["collection"], chunk["id"], chunk["text"], chunk["file_metadata"]) for chunk in added_chunks]
    )

    return graph_client.graph

async def process_text(chunk_collection: str, chunk_id: str, input_text: str, file_metadata: dict):
    print(f"Processing document ({file_metadata['id']}).")

    graph_client = await get_graph_client(infrastructure_config.get_config()["graph_engine"])

    document_id = await add_document_node(
        graph_client,
        parent_node_id = f"DefaultGraphModel__{USER_ID}", #make a param of defaultgraph model to make sure when user passes his stuff, it doesn't break pipeline
        document_metadata = file_metadata,
    )

    await add_label_nodes(graph_client, document_id, chunk_id, file_metadata["keywords"].split("|"))

    classified_categories = await get_content_categories(input_text)
    await add_classification_nodes(
        graph_client,
        parent_node_id = document_id,
        categories = classified_categories,
    )

    print(f"Document ({document_id}) classified.")

    content_summary = await get_content_summary(input_text)
    await add_summary_nodes(graph_client, document_id, content_summary)

    print(f"Document ({document_id}) summarized.")

    cognitive_layers = await get_cognitive_layers(input_text, classified_categories)
    cognitive_layers = (await add_cognitive_layers(graph_client, document_id, cognitive_layers))[:2]

    layer_graphs = await get_layer_graphs(input_text, cognitive_layers)
    await add_cognitive_layer_graphs(graph_client, chunk_collection, chunk_id, layer_graphs)

    if infrastructure_config.get_config()["connect_documents"] is True:
        db_engine = infrastructure_config.get_config()["database_engine"]
        relevant_documents_to_connect = db_engine.fetch_cognify_data(excluded_document_id = file_metadata["id"])

        print("Relevant documents to connect are: ", relevant_documents_to_connect)

        list_of_nodes = []

        relevant_documents_to_connect.append({
            "layer_id": document_id,
        })

        for document in relevant_documents_to_connect:
            node_descriptions_to_match = await graph_client.extract_node_description(document["layer_id"])
            list_of_nodes.extend(node_descriptions_to_match)

        print("List of nodes are: ", len(list_of_nodes))

        nodes_by_layer = await group_nodes_by_layer(list_of_nodes)
        print("Nodes by layer are: ", str(nodes_by_layer)[:5000])

        results = await resolve_cross_graph_references(nodes_by_layer)
        print("Results are: ", str(results)[:3000])

        relationships = graph_ready_output(results)

        await connect_nodes_in_graph(
            graph_client,
            relationships,
            score_threshold = infrastructure_config.get_config()["intra_layer_score_treshold"]
        )

        print(f"Document ({document_id}) cognified.")
