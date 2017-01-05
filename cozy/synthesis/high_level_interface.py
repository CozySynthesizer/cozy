from collections import namedtuple, deque, defaultdict
import datetime
import itertools

from cozy.common import typechecked, fresh_name, mk_map, pick_to_sum
from cozy.target_syntax import *
import cozy.syntax_tools
from cozy.syntax_tools import all_types, alpha_equivalent, BottomUpExplorer, free_vars, pprint, subst, implies, fresh_var, mk_lambda, all_exps
import cozy.incrementalization as inc
from cozy.typecheck import INT, BOOL
from cozy.timeouts import Timeout, TimeoutException

from . import core
from . import caching
from .rep_inference import infer_rep

SynthCtx = namedtuple("SynthCtx", ["all_types", "basic_types"])

@typechecked
def fragmentize(exp : Exp, out : [Exp], bound_names : {str} = set()):
    for e in all_exps(exp):
        if isinstance(e, ELambda):
            # lambdas may only appear in certain places---they aren't
            # first-class expressions---so we don't really want to see
            # them in the list of "all expressions"
            continue
        fvs = [fv for fv in free_vars(e) if fv.id not in bound_names]
        remap = { v.id : core.EHole(fresh_name(), v.type, None) for v in fvs }
        e = subst(e, remap)
        def allow_rename(v1, v2):
            return isinstance(v1, core.EHole) and v1.type == v2.type
        if not any(alpha_equivalent(e, root, allow_rename) for root in out):
            out.append(e)

def rename_args(queries : [Query]) -> [Query]:
    arg_hist = mk_map((a for q in queries for (a, t) in q.args), v=len)
    res = []
    for q in queries:
        arg_remap = { a : EVar(fresh_name(a)).with_type(t) for (a, t) in q.args if arg_hist[a] > 1 }
        if arg_remap:
            q = Query(
                q.name,
                tuple((arg_remap.get(a, EVar(a)).id, t) for (a, t) in q.args),
                subst(q.assumptions, arg_remap),
                subst(q.ret, arg_remap))
        res.append(q)
    return res

@typechecked
def get_roots(state : [EVar], e : Exp) -> [Exp]:
    state_var_names = set(v.id for v in state)
    roots = [
        EBool(True).with_type(BOOL),
        EBool(False).with_type(BOOL),
        # ENum(0).with_type(INT),
        # ENum(1).with_type(INT),
        ]
    fragmentize(e, roots, bound_names=state_var_names)
    return list(roots)

@typechecked
def guess_constructors(state : [EVar], roots : [Exp]) -> [Exp]:

    res = list(state)

    for sv in state:
        if isinstance(sv.type, TBag):
            ht = sv.type.t
            projs = []
            for r in roots:
                holes = list(core.find_holes(r))
                if len(holes) == 1 and holes[0].type == ht:
                    projs.append(mk_lambda(ht, lambda v: subst(r, { holes[0].name : v })))

            for p in projs:
                coll_hole = EHole(fresh_name(), sv.type, None)
                res.append(EMakeMap(
                    sv,
                    p,
                    mk_lambda(sv.type, lambda x: x)).with_type(TMap(p.body.type, sv.type)))
                res.append(EMap(coll_hole, p).with_type(TBag(p.body.type)))
                if p.body.type == BOOL:
                    # TODO: clauses instead
                    res.append(EFilter(coll_hole, p).with_type(sv.type))

    # for r in res:
    #     print("   --> {}".format(pprint(r)))
    return res

class BinderBuilder(core.Builder):
    def __init__(self, binders, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.binders = binders
    def build(self, cache, size):
        yield from super().build(cache, size)
        # print("  CACHE")
        # for (x, size) in cache:
        #     print("    " + pprint(x))
        if size >= 3:
            for (sz1, sz2) in pick_to_sum(2, size - 1):
                for bag in cache.find(type=TBag, size=sz1):
                    # if not isinstance(bag, EMapGet):
                    #     print("-----> " + pprint(bag) + " : " + pprint(bag.type))
                    #     continue
                    # print("###> " + pprint(bag) + " : " + pprint(bag.type))
                    for binder in self.binders:
                        if binder.type == bag.type.t:
                            # len
                            len = EUnaryOp("sum", EMap(bag, ELambda(binder, ENum(1).with_type(INT))).with_type(TBag(INT))).with_type(INT)
                            yield len
                            # empty?
                            yield EBinOp(len, "==", ENum(0).with_type(INT)).with_type(BOOL)
                            # for body in cache.find(size=sz2):
                            #     yield EMap(bag, ELambda(binder, body)).with_type(TBag(body.type))
                            for body in cache.find(size=sz2, type=BOOL):
                                yield EFilter(bag, ELambda(binder, body)).with_type(bag.type)
                            for body in cache.find(size=sz2, type=TBag):
                                yield EFlatMap(bag, ELambda(binder, body)).with_type(body.type)
        for t in list(cache.types()):
            if isinstance(t, TBag):
                yield EEmptyList().with_type(t)
                for e in cache.find(type=t.t, size=size-1):
                    yield ESingleton(e).with_type(t)

class CoolCostModel(core.CostModel):
    def __init__(self, state_vars : [EVar]):
        self.state_vars = state_vars
        self.rtcm = core.RunTimeCostModel()
        self.memcm = core.MemoryUsageCostModel()
        self.factor = 0.01 # 0 = only care about runtime, 1 = only care about memory
    def is_monotonic(self):
        return self.rtcm.is_monotonic() and self.memcm.is_monotonic()
    def split_cost(self, st, e):
        return (1-self.factor) * self.rtcm.cost(e) + self.factor * sum(self.memcm.cost(proj) for (v, proj) in st)
    def best_case_cost(self, e):
        try:
            return min((self.split_cost(rep, e2) for (rep, e2) in infer_rep(self.state_vars, e)),
                default=float("inf"))
        except:
            print("cost evaluation failed for {}".format(pprint(e)))
            print(repr(e))
            for (rep, e2) in infer_rep(self.state_vars, e):
                try:
                    self.split_cost(rep, e2)
                except Exception as exc:
                    print("-" * 20 + " EXCEPTION: {}".format(repr(exc)))
                    for (v, proj) in rep:
                        print("  {} : {} = {}".format(v.id, pprint(v.type), pprint(proj)))
                    print("  return {}".format(repr(e2)))
            raise

@typechecked
def synthesize_queries(ctx : SynthCtx, state : [EVar], assumptions : [Exp], q : Query, timeout : Timeout) -> (EVar, Exp, [Query]):
    """
    Synthesize efficient re-implementations for the given queries.

    Input:
        ctx         - a synthesis context for the problem
        state       - list of state variables
        assumptions - a list of global assumptions (i.e. not including q.assumptions)
        q           - a query to improve

    Output:
        (new_state, state_proj, new_queries)
    where
        new_state is a variable
        state_proj is an expression mapping state to new_state
        new_queries is a list of new query expressions
    """
    # q, = rename_args([q])
    assumptions = assumptions + list(q.assumptions)
    all_types = ctx.all_types
    basic_types = ctx.basic_types

    binders = []
    n_binders = 1 # TODO?
    for t in all_types:
        if isinstance(t, TBag):
            binders += [fresh_var(t.t) for i in range(n_binders)]
    print(binders)

    roots = get_roots(state, q.ret)
    ctors = guess_constructors(state, roots)

    for e in roots + ctors:
        print(" --> {}".format(pprint(e)))

    args = [EVar(name).with_type(t) for (name, t) in q.args]

    b = BinderBuilder(binders, roots + binders + ctors + args, basic_types, cost_model=CoolCostModel(state))
    new_state_vars = state
    state_proj_exprs = state
    new_ret = q.ret
    try:
        for expr in itertools.chain([q.ret], core.improve(
                target=q.ret,
                assumptions=EAll(assumptions),
                binders=binders,
                vars=state+args+binders,
                cost_model=b.cost_model(),
                builder=b,
                timeout=timeout)):

            print("SOLUTION")
            print("-" * 40)

            for (st, expr) in infer_rep(state, expr):
                for (sv, proj) in st:
                    print("  {} : {} = {}".format(sv.id, pprint(sv.type), pprint(proj)))
                print("  return {}".format(pprint(expr)))

                new_state_vars, state_proj_exprs = zip(*st) if st else ([], [])
                new_ret = expr
                print("-" * 40)

    except TimeoutException:
        print("stopping due to timeout")

    if len(new_state_vars) != 1:
        new_state_var = fresh_var(TTuple(tuple(v.type for v in new_state_vars)))
        state_proj_expr = ETuple(tuple(state_proj_exprs)).with_type(new_state_var.type)
        if new_state_vars:
            new_ret = subst(new_ret, {
                new_state_vars[i].id: ETupleGet(new_state_var, i)
                for i in range(len(new_state_vars)) })
    else:
        new_state_var = new_state_vars[0]
        state_proj_expr = state_proj_exprs[0]

    return (new_state_var, state_proj_expr, [Query(q.name, q.args, q.assumptions, new_ret)])

@typechecked
def synthesize(
        spec      : Spec,
        use_cache : bool = True,
        per_query_timeout : datetime.timedelta = datetime.timedelta(seconds=60)) -> (Spec, dict):
    """
    Main synthesis routine.

    Returns refined specification with better asymptotic performance, plus a
    dictionary mapping new state variables to their expressions in terms of
    original state variables.
    """

    # gather root types
    types = list(all_types(spec))
    basic_types = set(t for t in types if not isinstance(t, TBag))
    basic_types |= { BOOL, INT }
    print("basic types:")
    for t in basic_types:
        print("  --> {}".format(pprint(t)))
    basic_types = list(basic_types)
    ctx = SynthCtx(all_types=types, basic_types=basic_types)

    # collect state variables
    state_vars = [EVar(name).with_type(t) for (name, t) in spec.statevars]

    # collect queries
    qs = [q for q in spec.methods if isinstance(q, Query)]

    worklist = deque(qs)
    new_statevars = []
    state_var_exps = { }
    new_qs = []
    op_stms = defaultdict(list)

    op_deltas = { op.name : inc.to_delta(spec.statevars, op) for op in spec.methods if isinstance(op, Op) }

    global_assumptions = list(spec.assumptions)
    for v in state_vars:
        if isinstance(v.type, TBag) and isinstance(v.type.t, THandle):
            global_assumptions.append(EUnaryOp("unique", v).with_type(BOOL))

    # synthesis
    while worklist:
        q = worklist.popleft()
        print("##### SYNTHESIZING {}".format(q.name))

        cached_result = caching.find_cached_result(state_vars, global_assumptions, q) if use_cache else None
        if cached_result:
            print("##### FOUND CACHED RESULT")
            state_var, state_exp, new_q = cached_result
        else:
            state_var, state_exp, new_q = synthesize_queries(ctx, state_vars, global_assumptions, q, Timeout(per_query_timeout))
            new_q = new_q[0]
            if use_cache:
                caching.cache((state_vars, global_assumptions, q), (state_var, state_exp, new_q))

        print("  -> {} : {} = {}".format(state_var.id, pprint(state_var.type), pprint(state_exp)))
        print("  -> return {}".format(pprint(new_q.ret)))

        new_statevars.append((state_var.id, state_var.type))
        state_var_exps[state_var.id] = state_exp
        new_qs.append(new_q)

        for op in spec.methods:
            if isinstance(op, Op):
                print("###### INCREMENTALIZING: {}".format(op.name))
                (member, delta) = op_deltas[op.name]
                # print(member, delta)
                (state_update, subqueries) = inc.derivative(state_exp, member, delta, state_vars)
                # print(state_update, subqueries)
                state_update_stm = inc.apply_delta_in_place(state_var, state_update)
                # print(pprint(state_update_stm))
                op_stms[op.name].append(state_update_stm)
                for sub_q in subqueries:
                    print("########### SUBGOAL: {}".format(pprint(sub_q)))
                    worklist.append(sub_q)

    new_ops = []
    for op in spec.methods:
        if isinstance(op, Op):
            if isinstance(op_deltas[op.name][1], inc.BagElemUpdated):
                op_stms[op.name].append(op.body)
            new_stms = seq(op_stms[op.name])
            new_ops.append(Op(
                op.name,
                op.args,
                [],
                new_stms))

    return (Spec(
        spec.name,
        spec.types,
        spec.extern_funcs,
        new_statevars,
        [],
        new_ops + new_qs), state_var_exps)
