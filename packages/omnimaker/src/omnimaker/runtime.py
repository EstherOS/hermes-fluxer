"""Session runtime — drives the graph, routes envelopes, manages node instances."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import AsyncIterator

from omnimaker.adapters.base import NodeAdapter
from omnimaker.adapters.registry import registry as _registry
from omnimaker.graph import Graph, NodeInstance, RouteTable
from omnimaker.types import (
    CancelError,
    Endpoint,
    Envelope,
    ExecutionHandle,
    ProfileConfig,
    Route,
)

log = logging.getLogger(__name__)


class Invocation:
    """Tracks a pending invocation request/response lifecycle."""

    def __init__(self, route: Route, request: Envelope) -> None:
        self.route = route
        self.request = request
        self._response_future: asyncio.Future[Envelope] = asyncio.Future()

    async def wait(self) -> Envelope:
        """Await the response (used for sync invocations)."""
        return await self._response_future

    def resolve(self, response: Envelope) -> None:
        if not self._response_future.done():
            self._response_future.set_result(response)

    def cancel(self) -> None:
        if not self._response_future.done():
            self._response_future.set_exception(CancelError("invocation cancelled"))
    @property
    def done(self) -> bool:
        return self._response_future.done()


class Session:
    """A running omni graph session.

    One session per external interaction (Fluxer call, chat conversation).
    Routes envelopes between node instances according to the profile.
    """

    def __init__(self, profile: ProfileConfig) -> None:
        self.profile = profile
        self.graph = Graph(profile)
        self.id: str = uuid.uuid4().hex[:12]
        self._turn_counter: int = 0
        self._execution_counter: int = 0
        self._pending_invocations: dict[str, Invocation] = {}
        self._active_tasks: set[asyncio.Task] = set()
        self._execution_tasks: dict[str, asyncio.Task] = {}  # exec id → driving task
        self._execution_turns: dict[str, str | None] = {}    # exec id → turn id
        self._running = False
        self._lock = asyncio.Lock()

        # Backlog queues for busy nodes
        self._backlogs: dict[str, asyncio.Queue] = {}

        # Adapter instances (lazy-created)
        self._adapters: dict[str, NodeAdapter] = {}

        # Binding adapter instances (lazy-created, keyed by binding name)
        self._binding_adapters: dict[str, NodeAdapter] = {}

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Open all persistent node adapters and begin processing."""
        self._running = True
        for name, instance in self.graph.nodes.items():
            cfg = instance.config
            if cfg.session == "persistent":
                await self._ensure_adapter(name)

        log.info("Session %s started (profile=%s)", self.id, self.profile.name)

    async def stop(self) -> None:
        """Stop the session, cancel active executions, close adapters."""
        self._running = False

        # Cancel pending invocations
        for inv in self._pending_invocations.values():
            inv.cancel()

        # Cancel active node executions (adapter cleanup + driving task)
        for name in list(self.graph.nodes.keys()):
            await self._cancel_executions(name)

        # Close adapters
        for adapter in self._adapters.values():
            await adapter.close()

        # Cancel background tasks
        for task in self._active_tasks:
            task.cancel()

        log.info("Session %s stopped", self.id)

    async def interrupt(self, turn_id: str | None = None) -> int:
        """Cancel active executions and drop their queued work.

        The engine-level interruption verb (barge-in, platform stop): every
        matching execution's driving task is cancelled — which closes the
        adapter's output generator (cleanup runs: streams close, subprocesses
        die) — and queued envelopes for the interrupted turn(s) are dropped.

        With *turn_id*, only that turn is affected; without, all active
        executions are interrupted. Returns executions cancelled.
        """
        cancelled = 0
        for exec_id, task in list(self._execution_tasks.items()):
            if turn_id is not None and self._execution_turns.get(exec_id) != turn_id:
                continue
            if not task.done():
                task.cancel()
                cancelled += 1

        for name in list(self._backlogs.keys()):
            self._purge_backlog(name, turn_id)

        log.info("Session %s: interrupt(turn=%s) cancelled %d execution(s)",
                 self.id, turn_id or "*", cancelled)
        return cancelled

    # ── Envelope injection ─────────────────────────────────────────────

    async def feed(self, source: str, envelope: Envelope) -> str:
        """Inject an envelope from an external endpoint or stream.

        *source* is the endpoint string (e.g. ``@fluxer.audio`` or a
        stream name). The envelope is routed to all matching destinations.

        Returns the turn id assigned to this feed call.
        """
        async with self._lock:
            self._turn_counter += 1
            envelope.turn_id = f"t{self._turn_counter}"
            envelope.session_id = self.id
            turn_id = envelope.turn_id

        ep = Endpoint.parse(source)
        await self._dispatch(ep, envelope)
        return turn_id

    async def feed_envelope(self, envelope: Envelope) -> None:
        """Inject a fully-formed envelope (source already set)."""
        ep = Endpoint.parse(envelope.source or "")
        await self._dispatch(ep, envelope)

    # ── Internal routing ───────────────────────────────────────────────

    async def _dispatch(self, source: Endpoint, envelope: Envelope) -> None:
        """Find matching routes and deliver the envelope."""
        routes = self.graph.route_table.match(source)
        if not routes:
            log.debug("No routes from %s for %s", source, envelope.type)

        tasks = []
        for route in routes:
            if not route.matches(envelope):
                continue
            tasks.append(self._deliver(route, envelope))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _deliver(self, route: Route, envelope: Envelope) -> None:
        """Deliver *envelope* along *route* to the destination node."""
        dest = route.dest
        dest_name = dest.node

        # Resolve stream endpoint for dest if it's a stream alias
        actual_dest = dest
        if not dest.is_external:
            stream_ep = self.graph.stream_endpoint(dest_name)
            if stream_ep is not None:
                actual_dest = stream_ep

        # External endpoint — deliver to the binding adapter
        if actual_dest.is_external:
            await self._deliver_external(actual_dest, route, envelope)
            return

        # Control endpoint — engine-executed on delivery; never reaches the adapter
        if dest.port == "cancel":
            cancelled = await self._cancel_executions(dest_name)
            purged = self._purge_backlog(dest_name)
            log.info("Control: cancel → %s (%d execution(s) cancelled, %d queued dropped)",
                     dest_name, cancelled, purged)
            return

        # Internal node delivery
        node_instance = self.graph.get_node(dest_name)
        cfg = node_instance.config

        # Determine concurrency mode: route override > node interrupt policy > queue.
        conc_mode = route.transport.concurrency.get("mode") if route.transport.concurrency else None
        if not conc_mode:
            conc = cfg.concurrency
            triggered = self._interrupt_triggered(conc, envelope)
            # Node policy: on_interrupt applies when the arrival triggers it
            # (interrupt_on scopes which sources trigger; empty = any arrival).
            conc_mode = conc.on_interrupt if triggered else "queue"

        # Check if node is busy
        is_busy = len(node_instance.active_executions) > 0

        if is_busy:
            if conc_mode == "drop":
                log.debug("Dropping envelope to %s (busy, drop mode)", dest_name)
                return
            elif conc_mode == "replace":
                await self._cancel_executions(dest_name)
            elif conc_mode == "queue":
                await self._backlog(route, envelope)
                return
            elif conc_mode == "parallel":
                pass  # Allow concurrent execution

        # Create execution handle and run
        handle = ExecutionHandle.create(
            session_id=self.id,
            node_name=dest_name,
            parent_id=envelope.execution_id,
        )
        node_instance.active_executions[handle.id] = handle

        async def _execute() -> None:
            """Drive one node execution — runs as a tracked task."""
            last_output: Envelope | None = None
            try:
                adapter = await self._ensure_adapter(dest_name)
                env = envelope
                if route.kind == "invocation":
                    # For invocation routes, present as invoke call
                    outputs = adapter.invoke(env, handle)
                else:
                    outputs = adapter.accept(env, handle)

                async for output in outputs:
                    last_output = output
                    output.session_id = self.id
                    output.execution_id = handle.id
                    output.parent_execution_id = envelope.execution_id

                    # Dispatch from the declared produces ports. An output may
                    # tag metadata["port"] to select exactly one of them;
                    # untagged outputs fan out to all.
                    produces = node_instance.config.produces
                    dispatch_ports = produces if produces else ["output"]
                    hint = (output.metadata or {}).get("port")
                    if hint in dispatch_ports:
                        dispatch_ports = [hint]
                    for port in dispatch_ports:
                        out_ep = Endpoint(node=dest_name, port=port)
                        output.source = str(out_ep)
                        await self._dispatch(out_ep, output)

                    # Handle invocation response
                    if route.kind == "invocation" and route.response_to:
                        inv_key = handle.id
                        pending = self._pending_invocations.pop(inv_key, None)
                        if pending and not pending.done:
                            pending.resolve(output)

            except CancelError:
                log.debug("Execution %s cancelled on %s", handle.id, dest_name)
            except Exception as exc:
                log.error("Error in node %s: %s", dest_name, exc)
                # Emit error envelope
                err_env = Envelope(
                    type="error",
                    payload={"node": dest_name, "error": str(exc)},
                    session_id=self.id,
                    turn_id=envelope.turn_id,
                    execution_id=handle.id,
                    source=str(actual_dest),
                )
                await self._dispatch(actual_dest, err_env)
                return

            # Async invocation: deliver the merged response to response.to
            # when the target's execution completes.
            if (route.kind == "invocation" and route.response_to
                    and (route.await_mode or "sync") == "async"):
                if last_output is not None:
                    last_output.type = route.response_as or "tool_result"
                    last_output.source = str(Endpoint(node=dest_name, port="output"))
                    hop = Route(
                        source=Endpoint(node=dest_name, port="output"),
                        dest=route.response_to,
                        kind="dataflow",
                    )
                    await self._deliver(hop, last_output)
                else:
                    log.warning("Async invocation %s → %s produced no output",
                                dest_name, route.response_to)

        # Executions run as tracked tasks so cancel()/interrupt() can stop
        # them: cancelling the task raises CancelledError into the running
        # adapter generator — its cleanup runs (streams close, subprocesses
        # die). A cancellation of the execution itself does not propagate
        # into the caller awaiting it.
        task = asyncio.create_task(_execute())
        self._execution_tasks[handle.id] = task
        self._execution_turns[handle.id] = envelope.turn_id
        if route.kind == "invocation" and (route.await_mode or "sync") == "async":
            # Async invocation: fire-and-forget. The target's response is
            # delivered by the execution itself on completion; the caller
            # continues immediately.
            def _cleanup(_t, _id=handle.id, _inst=node_instance):
                self._execution_tasks.pop(_id, None)
                self._execution_turns.pop(_id, None)
                _inst.active_executions.pop(_id, None)
            task.add_done_callback(_cleanup)
            return
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            caller_cancelled = current.cancelling() if current else 0
            if task.cancelled() and not caller_cancelled:
                log.debug("Execution %s cancelled on %s (interrupt)",
                          handle.id, dest_name)
            else:
                # The caller is being cancelled — don't orphan the execution.
                if not task.done():
                    task.cancel()
                raise
        finally:
            self._execution_tasks.pop(handle.id, None)
            self._execution_turns.pop(handle.id, None)
            node_instance.active_executions.pop(handle.id, None)

    async def _deliver_external(self, ep: Endpoint, route: Route,
                                 envelope: Envelope) -> None:
        """Deliver an envelope to a platform binding adapter.

        Binding adapters expose ``@binding.port`` endpoints. Find the
        adapter by binding name and call ``accept()`` to give it the
        envelope. The adapter's ``accept()`` may produce side effects
        (enqueue audio for playback, emit platform events) but should
        not yield output envelopes (external endpoints are sinks).
        """
        binding_name = ep.node.lstrip("@")
        adapter = self.get_binding(binding_name)
        if adapter is None:
            log.warning("No binding '%s' for external delivery %s", binding_name, ep)
            return

        handle = ExecutionHandle.create(
            session_id=self.id,
            node_name=binding_name,
            parent_id=envelope.execution_id,
        )
        try:
            async for _ in adapter.accept(envelope, handle):
                pass  # binding accept is a sink — outputs are discarded
        except Exception as exc:
            log.error("Binding adapter %s error: %s", binding_name, exc)

    def _purge_backlog(self, node_name: str, turn_id: str | None = None) -> int:
        """Drop queued (not-yet-started) arrivals for *node_name*.

        With *turn_id*, only envelopes from that turn are dropped.
        Returns the number of envelopes dropped.
        """
        queue = self._backlogs.get(node_name)
        if queue is None:
            return 0
        kept: list[tuple] = []
        dropped = 0
        while not queue.empty():
            try:
                _route, env = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if turn_id is None or env.turn_id == turn_id:
                dropped += 1
            else:
                kept.append((_route, env))
        for env in kept:
            queue.put_nowait(env)
        return dropped

    async def _cancel_executions(self, node_name: str) -> int:
        """Cancel all active executions on *node_name*.

        Invokes the adapter's cancel() contract (out-of-band cleanup) and
        cancels the driving task (which raises into the adapter generator).
        """
        instance = self.graph.get_node(node_name)
        adapter = self._adapters.get(node_name)
        cancelled = 0
        for exec_id in list(instance.active_executions.keys()):
            if adapter:
                await adapter.cancel(exec_id)
            task = self._execution_tasks.get(exec_id)
            if task is not None and not task.done():
                task.cancel()
            cancelled += 1
        return cancelled

    def _interrupt_triggered(self, conc, envelope: Envelope) -> bool:
        """Whether *envelope* triggers the node's interrupt policy.

        Empty ``interrupt_on`` treats any arrival as an interrupt candidate.
        Entries match the envelope's source ("node.port") by full string or
        by originating node.
        """
        if envelope.type == "interrupt_signal":
            return True
        if not conc.interrupt_on:
            return True
        src = envelope.source or ""
        src_node = src.rsplit(".", 1)[0] if "." in src else src
        return src in conc.interrupt_on or src_node in conc.interrupt_on

    # ── Adapter management ─────────────────────────────────────────────

    async def _ensure_adapter(self, node_name: str) -> NodeAdapter:
        """Lazy-create and open the adapter for *node_name*."""
        if node_name in self._adapters:
            return self._adapters[node_name]

        instance = self.graph.get_node(node_name)
        config = instance.config

        adapter_cls = self.graph.resolve_adapter(node_name)
        adapter = adapter_cls()
        await adapter.open(self.id, config.config)
        # Graph tools declared on the node are provisioned to the adapter so
        # it can present them to its model; the engine executes the routes.
        if config.tools:
            bind = getattr(adapter, "bind_tools", None)
            if callable(bind):
                bind([
                    {"name": t.name, "description": t.description, "await": t.await_mode}
                    for t in config.tools
                ])
        self._adapters[node_name] = adapter
        return adapter

    # ── Backlog queue ──────────────────────────────────────────────────

    async def _backlog(self, route: Route, envelope: Envelope) -> None:
        """Queue a delivery for a busy node.

        The route is stored with the envelope: redelivery must preserve the
        original destination — an envelope's source does not always route
        back to where it was headed (invocation responses have no routes).
        """
        dest_name = route.dest.node
        if dest_name not in self._backlogs:
            self._backlogs[dest_name] = asyncio.Queue(maxsize=100)
        q = self._backlogs[dest_name]
        try:
            q.put_nowait((route, envelope))
        except asyncio.QueueFull:
            log.warning("Backlog full for %s, dropping envelope", dest_name)

        # Start a consumer task if not already running
        task_name = f"backlog-{dest_name}"
        if not any(t.get_name() == task_name for t in self._active_tasks):
            task = asyncio.create_task(
                self._drain_backlog(dest_name, q),
                name=task_name,
            )
            self._active_tasks.add(task)
            task.add_done_callback(self._active_tasks.discard)

    async def _drain_backlog(self, dest_name: str,
                              queue: asyncio.Queue) -> None:
        """Process queued envelopes for a node, in arrival order.

        Holds each envelope until the node is idle, dispatches it, then
        moves on. Exits when the queue is empty — ``_backlog`` restarts
        the consumer on the next arrival. (The earlier re-queue-and-break
        behaviour stranded the tail of a burst and could reorder items,
        which streaming mouths expose immediately.)
        """
        while self._running:
            try:
                route, envelope = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                return
            instance = self.graph.get_node(dest_name)
            while self._running and len(instance.active_executions) > 0:
                await asyncio.sleep(0.05)
            if not self._running:
                queue.task_done()
                return
            await self._deliver(route, envelope)
            queue.task_done()

    # ── Utility ────────────────────────────────────────────────────────

    def is_running(self) -> bool:
        return self._running

    # ── Binding adapter access ─────────────────────────────────────────

    def get_binding(self, name: str) -> NodeAdapter | None:
        """Get or create a binding adapter instance."""
        if name in self._binding_adapters:
            return self._binding_adapters[name]
        from omnimaker.adapters.registry import registry as _reg
        try:
            cls = _reg.resolve_binding(name)
            adapter = cls()
            self._binding_adapters[name] = adapter
            return adapter
        except KeyError:
            return None