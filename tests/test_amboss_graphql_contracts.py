"""
Offline GraphQL schema contract test suite across all Amboss/Magma scripts:
- LNDg/amboss_pull.py
- Magma/magma_sale_process.py
- Magma/magma_market_fee.py
- Other/fee_adjuster.py

Parses source files via Python's `ast` module so collection has zero import-time
side effects (e.g. logging handlers or config reads).

Prevents regressions caused by Amboss Magma & Space API schema boundaries:
1. SimpleMarketOrder (list) vs MarketOrder (get_order)
2. SimpleMarketOffer (list) vs MarketOffer (get_offer)
3. Amboss Space API GetNodeDetails & Fee_info contracts
4. Magma mutation input type contracts
"""

import ast
import re
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
AMBOSS_PULL_PATH = REPO_ROOT / "LNDg" / "amboss_pull.py"
MAGMA_SALE_PROCESS_PATH = REPO_ROOT / "Magma" / "magma_sale_process.py"
MAGMA_MARKET_FEE_PATH = REPO_ROOT / "Magma" / "magma_market_fee.py"
FEE_ADJUSTER_PATH = REPO_ROOT / "Other" / "fee_adjuster.py"


@dataclass(frozen=True)
class ParsedModuleSource:
    constants: dict[str, str]
    functions: dict[str, str]


def _parse_module_source(path: Path) -> ParsedModuleSource:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    constants: dict[str, str] = {}
    functions: dict[str, str] = {}

    for node in tree.body:
        if isinstance(node, ast.Assign):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = node.value.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                constants[node.target.id] = node.value.value
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            segment = ast.get_source_segment(source, node)
            if segment:
                functions[node.name] = segment

    return ParsedModuleSource(constants=constants, functions=functions)


AMBOSS_PULL_SRC = _parse_module_source(AMBOSS_PULL_PATH)
MAGMA_SALE_PROCESS_SRC = _parse_module_source(MAGMA_SALE_PROCESS_PATH)
MAGMA_MARKET_FEE_SRC = _parse_module_source(MAGMA_MARKET_FEE_PATH)
FEE_ADJUSTER_SRC = _parse_module_source(FEE_ADJUSTER_PATH)


# --- Pinned Live Amboss Schema Field Sets ---

SIMPLE_MARKET_ORDER_FIELDS = {
    "amount",
    "asset",
    "channel_id",
    "created_at",
    "destination",
    "fees",
    "id",
    "options",
    "payment_status",
    "size",
    "source",
    "status",
    "timeout",
}

DETAIL_ONLY_MARKET_ORDER_FIELDS = {
    "blocks_until_can_be_closed",
    "confirmations",
    "is_seller",
    "payment",
    "promises",
    "simple_offer",
    "transaction_id",
}

SIMPLE_MARKET_OFFER_FIELDS = {
    "asset",
    "created_at",
    "fees",
    "filled_orders",
    "id",
    "locked_amount",
    "locked_size",
    "node",
    "promises",
    "status",
    "total_amount",
    "total_size",
}

DETAIL_ONLY_MARKET_OFFER_FIELDS = {
    "conditions",
    "is_owner",
    "max_amount",
    "max_size",
    "min_amount",
    "min_size",
    "onchain_multiplier",
    "onchain_priority",
    "orders",
}

NON_EXISTENT_OFFER_FIELDS = {
    "seller_score",
    "side",
    "offer_type",
}

VALID_FEE_INFO_FIELDS = {
    "max",
    "mean",
    "median",
    "weighted",
    "weighted_corrected",
}


# --- Helpers for Offline GraphQL Selection-Set Inspection ---


def _strip_comments(gql: str) -> str:
    return re.sub(r"#[^\n]*", "", gql)


def _extract_block(gql: str, field_name: str) -> str:
    """
    Finds `<field_name>(optional args) { ... }` in a GraphQL string and returns
    the inner content of that balanced brace block.
    """
    cleaned = _strip_comments(gql)
    pattern = re.compile(rf"\b{re.escape(field_name)}\s*(?:\([^)]*\))?\s*\{{")
    match = pattern.search(cleaned)
    assert match is not None, f"Could not find block '{field_name} {{ ... }}' in:\n{gql}"

    start_idx = match.end()
    depth = 1
    idx = start_idx
    while idx < len(cleaned) and depth > 0:
        ch = cleaned[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        idx += 1

    assert depth == 0, f"Unbalanced braces in block '{field_name}'"
    return cleaned[start_idx : idx - 1]


def _parse_immediate_fields(block_body: str) -> tuple[set[str], set[str]]:
    """
    Returns `(all_immediate_fields, scalar_immediate_fields)` at depth 0 of `block_body`.
    - `all_immediate_fields`: all field names selected directly on this type.
    - `scalar_immediate_fields`: field names at depth 0 that do NOT open a `{ ... }` sub-selection.
    """
    cleaned = _strip_comments(block_body)
    no_args = re.sub(r"\([^)]*\)", " ", cleaned)

    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\{|\}", no_args)
    immediate_fields: set[str] = set()
    scalar_fields: set[str] = set()
    depth = 0

    for i, tok in enumerate(tokens):
        if tok == "{":
            depth += 1
        elif tok == "}":
            depth -= 1
        elif depth == 0:
            immediate_fields.add(tok)
            next_tok = tokens[i + 1] if i + 1 < len(tokens) else None
            if next_tok != "{":
                scalar_fields.add(tok)

    return immediate_fields, scalar_fields


def _resolve_func_graphql(
    parsed_mod: ParsedModuleSource, func_name: str, fallback_const_name: str | None = None
) -> str:
    """
    Extracts the GraphQL query/mutation used by `func_name` in `parsed_mod`, either from inline
    strings in the function body or from module-level GraphQL constants referenced by the function.
    """
    assert func_name in parsed_mod.functions, f"Function '{func_name}' not found in module"
    src = parsed_mod.functions[func_name]
    parts = [src]
    if fallback_const_name and fallback_const_name in parsed_mod.constants:
        parts.append(parsed_mod.constants[fallback_const_name])
    for attr_name in re.findall(r"\b[A-Z][A-Z0-9_]{3,}\b", src):
        val = parsed_mod.constants.get(attr_name)
        if isinstance(val, str) and ("query" in val or "mutation" in val or "{" in val):
            parts.append(val)
    return "\n".join(parts)


# --- 3a. SimpleMarketOrder vs MarketOrder Boundary ---


def test_simple_market_order_vs_market_order_boundary():
    """
    Verifies that list queries (sales.list) only select fields valid on SimpleMarketOrder
    and never select detail-only MarketOrder fields (nor `sockets` on SimpleNode),
    whereas single-order queries use `get_order` (MarketOrder).
    """
    # 1. LNDg/amboss_pull.py GET_SALES_PAGE_QUERY
    sales_list_body = _extract_block(AMBOSS_PULL_SRC.constants["GET_SALES_PAGE_QUERY"], "list")
    fields, _ = _parse_immediate_fields(sales_list_body)
    assert fields, "GET_SALES_PAGE_QUERY list selection must not be empty"
    assert fields <= SIMPLE_MARKET_ORDER_FIELDS, (
        f"GET_SALES_PAGE_QUERY selects fields not on SimpleMarketOrder: {fields - SIMPLE_MARKET_ORDER_FIELDS}"
    )
    assert not (fields & DETAIL_ONLY_MARKET_ORDER_FIELDS)

    # 2. Magma/magma_sale_process.py ListOrders queries in get_offers_awaiting_seller_approval
    # and get_orders_awaiting_channel_open
    for func_name in ("get_offers_awaiting_seller_approval", "get_orders_awaiting_channel_open"):
        gql_source = _resolve_func_graphql(MAGMA_SALE_PROCESS_SRC, func_name, "GET_SALES_QUERY")
        list_body = _extract_block(gql_source, "list")
        list_fields, _ = _parse_immediate_fields(list_body)
        assert list_fields, f"{func_name} list selection must not be empty"
        assert list_fields <= SIMPLE_MARKET_ORDER_FIELDS, (
            f"{func_name} selects fields not on SimpleMarketOrder: {list_fields - SIMPLE_MARKET_ORDER_FIELDS}"
        )
        assert not (list_fields & DETAIL_ONLY_MARKET_ORDER_FIELDS), (
            f"{func_name} must not select detail-only MarketOrder fields: {list_fields & DETAIL_ONLY_MARKET_ORDER_FIELDS}"
        )
        # Ensure `sockets` is not selected on SimpleNode (destination/source) inside list
        assert "sockets" not in list_body, f"{func_name} must not select 'sockets' on SimpleNode inside list"

    # 3. Detail queries must query `get_order` (MarketOrder)
    lease_detail_body = _extract_block(
        AMBOSS_PULL_SRC.constants["GET_ORDER_LEASE_DETAILS_QUERY"], "get_order"
    )
    lease_fields, _ = _parse_immediate_fields(lease_detail_body)
    assert {"id", "status", "channel_id", "blocks_until_can_be_closed", "promises"} <= lease_fields

    sale_detail_source = _resolve_func_graphql(
        MAGMA_SALE_PROCESS_SRC, "get_order_details_from_amboss", "GET_ORDER_DETAILS_QUERY"
    )
    order_detail_body = _extract_block(sale_detail_source, "get_order")
    order_detail_fields, _ = _parse_immediate_fields(order_detail_body)
    assert "promises" in order_detail_fields or "transaction_id" in order_detail_fields


# --- 3b. SimpleMarketOffer vs MarketOffer Boundary ---


def test_simple_market_offer_vs_market_offer_boundary():
    """
    Verifies GET_PUBLIC_MAGMA_OFFERS_QUERY and GET_MY_MAGMA_OFFERS_QUERY only select
    fields valid on SimpleMarketOffer (never DETAIL_ONLY_MARKET_OFFER_FIELDS or
    NON_EXISTENT_OFFER_FIELDS, and never `asset` as a bare scalar), while
    GET_MY_OFFER_DETAILS_QUERY queries `get_offer` (MarketOffer) and selects
    `min_amount` and `max_amount`.
    """
    for query_name in ("GET_PUBLIC_MAGMA_OFFERS_QUERY", "GET_MY_MAGMA_OFFERS_QUERY"):
        query_str = MAGMA_MARKET_FEE_SRC.constants[query_name]
        list_body = _extract_block(query_str, "list")
        fields, scalar_fields = _parse_immediate_fields(list_body)
        assert fields, f"{query_name} list selection must not be empty"
        assert fields <= SIMPLE_MARKET_OFFER_FIELDS, (
            f"{query_name} selects fields not on SimpleMarketOffer: {fields - SIMPLE_MARKET_OFFER_FIELDS}"
        )
        assert not (fields & DETAIL_ONLY_MARKET_OFFER_FIELDS), (
            f"{query_name} selects detail-only MarketOffer fields: {fields & DETAIL_ONLY_MARKET_OFFER_FIELDS}"
        )
        assert not (fields & NON_EXISTENT_OFFER_FIELDS), (
            f"{query_name} selects non-existent offer fields: {fields & NON_EXISTENT_OFFER_FIELDS}"
        )
        assert "asset" not in scalar_fields, (
            f"{query_name} must not select 'asset' as a bare scalar (ChannelAsset! requires subfields)"
        )

    assert "GET_MY_OFFER_DETAILS_QUERY" in MAGMA_MARKET_FEE_SRC.constants, (
        "magma_market_fee must define GET_MY_OFFER_DETAILS_QUERY for full MarketOffer details"
    )
    offer_detail_body = _extract_block(
        MAGMA_MARKET_FEE_SRC.constants["GET_MY_OFFER_DETAILS_QUERY"], "get_offer"
    )
    detail_fields, _ = _parse_immediate_fields(offer_detail_body)
    assert {"min_amount", "max_amount"} <= detail_fields, (
        f"GET_MY_OFFER_DETAILS_QUERY must select min_amount and max_amount on get_offer, got: {detail_fields}"
    )


# --- 3c. Amboss Space API GetNodeDetails & Fee_info Contracts ---


def test_amboss_space_get_node_details_contract():
    """
    Verifies get_node_extended_details in Magma/magma_sale_process.py declares `$from: String!`,
    calls `channel_closure_types(from: $from)`, queries `score` under `terminal_web`,
    and does NOT query the removed `bos_score` field.
    """
    node_details_src = _resolve_func_graphql(MAGMA_SALE_PROCESS_SRC, "get_node_extended_details")
    cleaned_node_src = _strip_comments(node_details_src)

    assert "$from: String!" in cleaned_node_src, (
        "get_node_extended_details must declare `$from: String!` in its GraphQL query"
    )
    assert re.search(r"channel_closure_types\s*\(\s*from\s*:\s*\$from\s*\)", cleaned_node_src), (
        "get_node_extended_details must call `channel_closure_types(from: $from)`"
    )
    terminal_web_body = _extract_block(cleaned_node_src, "terminal_web")
    terminal_web_fields, _ = _parse_immediate_fields(terminal_web_body)
    assert "score" in terminal_web_fields, (
        f"terminal_web must select 'score', got: {terminal_web_fields}"
    )
    assert "bos_score" not in cleaned_node_src, (
        "get_node_extended_details must not query the removed 'bos_score' field"
    )


def test_amboss_space_fee_info_contract():
    """
    Verifies fetch_amboss_data in Other/fee_adjuster.py queries `getNode(pubkey: $pubkey)` ->
    `graph_info` -> `channels` -> `fee_info(timeRange: $timeRange)` -> `remote`
    with valid FeeInfo fields (`max`, `mean`, `median`, `weighted`, `weighted_corrected`).
    """
    fee_src = _strip_comments(FEE_ADJUSTER_SRC.functions["fetch_amboss_data"])
    assert re.search(r"getNode\s*\(\s*pubkey\s*:\s*\$pubkey\s*\)", fee_src)
    get_node_body = _extract_block(fee_src, "getNode")
    graph_info_body = _extract_block(get_node_body, "graph_info")
    channels_body = _extract_block(graph_info_body, "channels")
    assert re.search(r"fee_info\s*\(\s*timeRange\s*:\s*\$timeRange\s*\)", channels_body)
    fee_info_body = _extract_block(channels_body, "fee_info")
    remote_body = _extract_block(fee_info_body, "remote")
    remote_fields, _ = _parse_immediate_fields(remote_body)
    assert remote_fields == VALID_FEE_INFO_FIELDS, (
        f"fetch_amboss_data remote FeeInfo fields mismatch: {remote_fields} != {VALID_FEE_INFO_FIELDS}"
    )


# --- 3d. Magma Mutations Input Contracts ---


def test_magma_mutations_input_contracts():
    """
    Verifies Magma GraphQL mutations declare the exact live input types:
    - CREATE_MAGMA_OFFER_MUTATION: CreateOfferInput!
    - UPDATE_MAGMA_OFFER_MUTATION: UpdateOfferInput!
    - TOGGLE_MAGMA_OFFER_MUTATION: ToggleOfferInput!
    - accept_order: SellerAcceptOrdersInput!
    - reject_order: SellerRejectOrdersInput!
    - confirm_channel_point_to_amboss: SellerAddTransactionInput!
    """
    assert "CreateOfferInput!" in MAGMA_MARKET_FEE_SRC.constants["CREATE_MAGMA_OFFER_MUTATION"]
    assert "UpdateOfferInput!" in MAGMA_MARKET_FEE_SRC.constants["UPDATE_MAGMA_OFFER_MUTATION"]
    assert "ToggleOfferInput!" in MAGMA_MARKET_FEE_SRC.constants["TOGGLE_MAGMA_OFFER_MUTATION"]

    accept_src = _resolve_func_graphql(
        MAGMA_SALE_PROCESS_SRC, "accept_order", "ACCEPT_ORDER_MUTATION"
    )
    reject_src = _resolve_func_graphql(
        MAGMA_SALE_PROCESS_SRC, "reject_order", "REJECT_ORDER_MUTATION"
    )
    confirm_src = _resolve_func_graphql(
        MAGMA_SALE_PROCESS_SRC, "confirm_channel_point_to_amboss", "ADD_TRANSACTION_MUTATION"
    )

    assert "SellerAcceptOrdersInput!" in accept_src
    assert "SellerRejectOrdersInput!" in reject_src
    assert "SellerAddTransactionInput!" in confirm_src
