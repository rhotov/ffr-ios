using Ryujinx.Graphics.Shader.IntermediateRepresentation;
using System.Collections.Generic;

namespace Ryujinx.Graphics.Shader.Translation.Optimizations
{
    static class Utils
    {
        public static bool IsInputLoad(INode node)
        {
            return (node is Operation operation) &&
                   operation.Inst == Instruction.Load &&
                   operation.StorageKind == StorageKind.Input;
        }

        public static bool IsInputLoad(INode node, IoVariable ioVariable, int elemIndex)
        {
            if (node is not Operation operation ||
                operation.Inst != Instruction.Load ||
                operation.StorageKind != StorageKind.Input ||
                operation.SourcesCount != 2)
            {
                return false;
            }

            Operand ioVariableSrc = operation.GetSource(0);

            if (ioVariableSrc.Type != OperandType.Constant || (IoVariable)ioVariableSrc.Value != ioVariable)
            {
                return false;
            }

            Operand elemIndexSrc = operation.GetSource(1);

            return elemIndexSrc.Type == OperandType.Constant && elemIndexSrc.Value == elemIndex;
        }

        private static bool IsSameOperand(Operand x, Operand y)
        {
            if (x.Type != y.Type || x.Value != y.Value)
            {
                return false;
            }

            // TODO: Handle Load operations with the same storage and the same constant parameters.
            return x == y || x.Type == OperandType.Constant || x.Type == OperandType.ConstantBuffer;
        }

        private static bool AreAllSourcesEqual(Operation node, Operation otherNode)
        {
            if (node.SourcesCount != otherNode.SourcesCount)
            {
                return false;
            }

            for (int index = 0; index < node.SourcesCount; index++)
            {
                if (!IsSameOperand(node.GetSource(index), otherNode.GetSource(index)))
                {
                    return false;
                }
            }

            return true;
        }

        public static bool AreAllSourcesTheSameOperand(INode node)
        {
            Operand firstSrc = node.GetSource(0);

            for (int index = 1; index < node.SourcesCount; index++)
            {
                if (!IsSameOperand(firstSrc, node.GetSource(index)))
                {
                    return false;
                }
            }

            return true;
        }

        private static Operation FindBranchSource(BasicBlock block)
        {
            foreach (BasicBlock sourceBlock in block.Predecessors)
            {
                if (sourceBlock.Operations.Count > 0)
                {
                    if (sourceBlock.GetLastOp() is Operation lastOp && IsConditionalBranch(lastOp.Inst) && sourceBlock.Next == block)
                    {
                        return lastOp;
                    }
                }
            }

            return null;
        }

        private static bool IsConditionalBranch(Instruction inst)
        {
            return inst is Instruction.BranchIfFalse or Instruction.BranchIfTrue;
        }

        private static bool IsSameCondition(Operand currentCondition, Operand queryCondition)
        {
            if (currentCondition == queryCondition)
            {
                return true;
            }

            return currentCondition.AsgOp is Operation currentOperation &&
                queryCondition.AsgOp is Operation queryOperation &&
                currentOperation.Inst == queryOperation.Inst &&
                currentOperation.StorageKind == queryOperation.StorageKind &&
                currentOperation.Index == queryOperation.Index &&
                currentOperation.ForcePrecise == queryOperation.ForcePrecise &&
                currentOperation.DestsCount == 1 && queryOperation.DestsCount == 1 &&
                IsPureConditionOperation(currentOperation) && IsPureConditionOperation(queryOperation) &&
                AreAllSourcesEqual(currentOperation, queryOperation);
        }

        private static bool IsPureConditionOperation(Operation operation)
        {
            // Exact SSA identity is handled before this comparison. Different
            // operations may only be equated when they are pure with identical
            // metadata and stable SSA inputs; mutable evaluations require their
            // own control-flow epoch proof, never an address-only comparison.
            return (operation.Inst & Instruction.Mask) switch
            {
                Instruction.Copy or Instruction.BitwiseAnd or Instruction.BitwiseOr or
                Instruction.BitwiseExclusiveOr or Instruction.BitwiseNot or
                Instruction.LogicalAnd or Instruction.LogicalOr or Instruction.LogicalExclusiveOr or Instruction.LogicalNot or
                Instruction.ShiftLeft or Instruction.ShiftRightS32 or Instruction.ShiftRightU32 or
                Instruction.CompareEqual or Instruction.CompareNotEqual or
                Instruction.CompareLess or Instruction.CompareLessOrEqual or Instruction.CompareLessU32 or Instruction.CompareLessOrEqualU32 or
                Instruction.CompareGreater or Instruction.CompareGreaterOrEqual or Instruction.CompareGreaterU32 or Instruction.CompareGreaterOrEqualU32 => true,
                Instruction.Load => operation.StorageKind == StorageKind.ConstantBuffer,
                _ => false,
            };
        }

        private sealed class GuardedPhiContext
        {
            private readonly HashSet<BasicBlock> _ancestors = new();
            private readonly HashSet<BasicBlock> _cyclic = new();
            private readonly Dictionary<INode, BasicBlock> _owners = new();
            private readonly Dictionary<Operand, bool> _stable = new();
            private readonly Dictionary<Operand, bool> _boolean = new();
            private readonly List<(Operand condition, bool value)> _facts = new();
            private readonly List<(Operand condition, bool value, BasicBlock anchor, BasicBlock demandOwner)> _localFacts = new();
            private readonly Dictionary<(int, BasicBlock), bool> _epochs = new();
            private readonly Dictionary<int, HashSet<BasicBlock>> _localReachable = new();
            private readonly HashSet<BasicBlock> _reachable = new();
            private readonly BasicBlock _entry;
            private readonly BasicBlock _query;
            private readonly bool _provedGraph;
            private readonly bool _useLocalFacts;

            public GuardedPhiContext(BasicBlock block, bool useLocalFacts = true)
            {
                _query = block;
                _useLocalFacts = useLocalFacts;
                Stack<BasicBlock> pending = new(); pending.Push(block);
                while (pending.TryPop(out BasicBlock current))
                {
                    if (!_ancestors.Add(current)) continue;
                    if (current.Index == 0) _entry = current;
                    foreach (INode node in current.Operations) _owners[node] = current;
                    foreach (BasicBlock predecessor in current.Predecessors) pending.Push(predecessor);
                }
                if (_entry == null || !CanReachQuery(null, null)) return;
                FindCyclicBlocks();

                // Dominator metadata only limits candidate discovery. Every
                // claimed edge fact is re-proved against the actual current CFG.
                HashSet<BasicBlock> candidates = new();
                foreach (BasicBlock predecessor in block.Predecessors) candidates.Add(predecessor);
                BasicBlock dominator = block;
                HashSet<BasicBlock> walked = new();
                while (dominator != null && walked.Add(dominator))
                {
                    candidates.Add(dominator);
                    BasicBlock next = dominator.ImmediateDominator;
                    if (next == dominator) break;
                    dominator = next;
                }
                foreach (BasicBlock candidate in candidates)
                {
                    if (candidate.Next == candidate.Branch ||
                        candidate.GetLastOp() is not Operation branch || !IsConditionalBranch(branch.Inst)) continue;
                    Operand condition = branch.GetSource(0);
                    bool? value = null;
                    if (candidate.Next != null && !CanReachQuery(candidate, candidate.Next))
                        value = branch.Inst == Instruction.BranchIfFalse;
                    else if (candidate.Branch != null && !CanReachQuery(candidate, candidate.Branch))
                        value = branch.Inst == Instruction.BranchIfTrue;
                    if (value == null) continue;
                    if (IsStable(condition, new HashSet<Operand>())) AddFact(condition, value.Value, new HashSet<(Operand, bool)>());
                    else if (block.Predecessors.Count == 1 && block.Predecessors[0] == candidate &&
                        condition.AsgOp != null && _owners.TryGetValue(condition.AsgOp, out BasicBlock anchor))
                        _localFacts.Add((condition, value.Value, anchor, null));
                }
                CollectReachable();
                InferQueryPhiSelector();
                CollectReachable();
                _provedGraph = true;
            }

            private void CollectReachable()
            {
                _reachable.Clear();
                Stack<BasicBlock> pending = new();
                pending.Push(_entry);
                while (pending.TryPop(out BasicBlock current))
                {
                    if (!_ancestors.Contains(current) || !_reachable.Add(current)) continue;
                    if (current.Next != null && EdgeAllowed(current, current.Next)) pending.Push(current.Next);
                    if (current.Branch != null && EdgeAllowed(current, current.Branch)) pending.Push(current.Branch);
                }
            }

            private void InferQueryPhiSelector()
            {
                // Only this exact Phi evaluation immediately guards the query.
                // A constant input of the opposite truth cannot have been taken
                // on the current execution. Unknown inputs remain possible.
                if (_query.Predecessors.Count != 1) return;
                BasicBlock queryPredecessor = _query.Predecessors[0];
                if (queryPredecessor.Next == queryPredecessor.Branch ||
                    queryPredecessor.GetLastOp() is not Operation queryBranch || !IsConditionalBranch(queryBranch.Inst) ||
                    queryBranch.GetSource(0).AsgOp is not PhiNode phi) return;
                BasicBlock merge = phi.Block;
                if (merge == null || !_reachable.Contains(merge) || !merge.Operations.Contains(phi) ||
                    ReachWithout(_entry, _query, merge)) return;
                bool demand = queryPredecessor.Next == _query ? queryBranch.Inst == Instruction.BranchIfFalse :
                    queryBranch.Inst == Instruction.BranchIfTrue;
                HashSet<BasicBlock> predecessors = new(merge.Predecessors);
                if (predecessors.Count != 2) return;
                BasicBlock body = null;
                foreach (BasicBlock predecessor in predecessors)
                {
                    if ((predecessor.Next != merge && predecessor.Branch != merge) ||
                        !phi.TryGetIncomingSource(predecessor, out Operand input)) return;
                    if (input.Type == OperandType.Constant && (input.Value != 0) != demand) continue;
                    if (body != null) return;
                    body = predecessor;
                }
                if (body == null || body == merge || body.Predecessors.Count != 1 ||
                    (body.Next != null && body.Next != merge) ||
                    (body.Branch != null && body.Branch != merge)) return;
                BasicBlock selector = body.Predecessors[0];
                if (selector == merge || selector == body || !predecessors.Contains(selector) ||
                    selector.Next == selector.Branch ||
                    !((selector.Next == body && selector.Branch == merge) || (selector.Branch == body && selector.Next == merge)) ||
                    selector.GetLastOp() is not Operation branch || !IsConditionalBranch(branch.Inst)) return;
                Operand condition = branch.GetSource(0);
                bool value = selector.Branch == body ? branch.Inst == Instruction.BranchIfTrue :
                    branch.Inst == Instruction.BranchIfFalse;
                if (IsStable(condition, new HashSet<Operand>())) AddFact(condition, value, new HashSet<(Operand, bool)>());
                else if (condition.AsgOp != null && _owners.TryGetValue(condition.AsgOp, out BasicBlock anchor))
                    _localFacts.Add((condition, value, anchor, merge));
            }

            private bool CanReachQuery(BasicBlock cutFrom, BasicBlock cutTo)
            {
                HashSet<BasicBlock> seen = new(); Stack<BasicBlock> pending = new(); pending.Push(_entry);
                while (pending.TryPop(out BasicBlock current))
                {
                    if (!_ancestors.Contains(current) || !seen.Add(current)) continue;
                    if (current == _query) return true;
                    if (current.Next != null && !(current == cutFrom && current.Next == cutTo)) pending.Push(current.Next);
                    if (current.Branch != null && !(current == cutFrom && current.Branch == cutTo)) pending.Push(current.Branch);
                }
                return false;
            }

            private void FindCyclicBlocks()
            {
                Dictionary<BasicBlock, int> index = new(), low = new();
                Stack<BasicBlock> stack = new(); HashSet<BasicBlock> active = new(); int serial = 0;
                void Visit(BasicBlock block)
                {
                    index[block] = low[block] = serial++; stack.Push(block); active.Add(block);
                    foreach (BasicBlock next in new[] { block.Next, block.Branch })
                    {
                        if (next == null || !_ancestors.Contains(next)) continue;
                        if (!index.ContainsKey(next)) { Visit(next); low[block] = System.Math.Min(low[block], low[next]); }
                        else if (active.Contains(next)) low[block] = System.Math.Min(low[block], index[next]);
                    }
                    if (low[block] != index[block]) return;
                    List<BasicBlock> component = new(); BasicBlock member;
                    do { member = stack.Pop(); active.Remove(member); component.Add(member); } while (member != block);
                    if (component.Count > 1 || block.Next == block || block.Branch == block)
                        foreach (BasicBlock cyclic in component) _cyclic.Add(cyclic);
                }
                foreach (BasicBlock block in _ancestors) if (!index.ContainsKey(block)) Visit(block);
            }

            private bool IsStable(Operand value, HashSet<Operand> active)
            {
                if (value.Type is OperandType.Constant or OperandType.ConstantBuffer) return true;
                if (_stable.TryGetValue(value, out bool known)) return known;
                if (!active.Add(value)) return false;
                bool stable = false;
                // A definition outside every original CFG cycle executes once.
                // Inside a cycle, identity alone is insufficient: only pure
                // expressions of stable values may constrain other iterations.
                if (value.AsgOp != null && _owners.TryGetValue(value.AsgOp, out BasicBlock owner) && !_cyclic.Contains(owner)) stable = true;
                else if (value.AsgOp is Operation operation && operation is not TextureOperation && IsStableOperation(operation))
                {
                    stable = true;
                    for (int i = 0; i < operation.SourcesCount; i++)
                        if (!IsStable(operation.GetSource(i), active)) { stable = false; break; }
                }
                active.Remove(value); _stable[value] = stable; return stable;
            }

            private static bool IsStableOperation(Operation operation)
            {
                if (IsPureConditionOperation(operation)) return true;
                return (operation.Inst & Instruction.Mask) is
                    Instruction.Add or Instruction.Subtract or Instruction.Multiply or Instruction.Divide or
                    Instruction.Absolute or Instruction.Negate or Instruction.Minimum or Instruction.Maximum or
                    Instruction.MinimumU32 or Instruction.MaximumU32 or Instruction.Clamp or Instruction.ClampU32 or
                    Instruction.ConditionalSelect or Instruction.BitfieldExtractS32 or Instruction.BitfieldExtractU32 or
                    Instruction.BitfieldInsert or Instruction.ConvertFP32ToS32 or Instruction.ConvertFP32ToU32 or
                    Instruction.ConvertS32ToFP32 or Instruction.ConvertU32ToFP32;
            }

            private bool SameStableCondition(Operand condition, Operand other)
            {
                if (condition == other) return true;
                if (!IsSameCondition(condition, other)) return false;
                // Equal pure operations may consume an SSA Phi that changes on
                // loop iterations. Prove their shared inputs stable as well.
                Operation operation = (Operation)condition.AsgOp;
                for (int i = 0; i < operation.SourcesCount; i++)
                    if (!IsStable(operation.GetSource(i), new HashSet<Operand>())) return false;
                return true;
            }

            private bool EdgeAllowed(BasicBlock from, BasicBlock to)
            {
                if (from.GetLastOp() is Operation branch && IsConditionalBranch(branch.Inst) &&
                    TryEvaluateCondition(branch.GetSource(0), new HashSet<Operand>(), out bool value))
                {
                    bool takeBranch = value == (branch.Inst == Instruction.BranchIfTrue);
                    if (to != (takeBranch ? from.Branch : from.Next)) return false;
                }
                return to == from.Next || to == from.Branch;
            }

            private void AddFact(Operand condition, bool value, HashSet<(Operand, bool)> seen)
            {
                if (!seen.Add((condition, value)) || !IsStable(condition, new HashSet<Operand>())) return;
                _facts.Add((condition, value));
                if (condition.AsgOp is not Operation operation) return;
                Instruction logical = BooleanOperation(operation);
                if ((logical == Instruction.LogicalAnd && value) || (logical == Instruction.LogicalOr && !value))
                {
                    AddFact(operation.GetSource(0), value, seen); AddFact(operation.GetSource(1), value, seen);
                }
                else if (logical == Instruction.LogicalNot) AddFact(operation.GetSource(0), !value, seen);
            }

            private bool IsBooleanValue(Operand value, HashSet<Operand> active)
            {
                if (value.Type == OperandType.Constant) return value.Value is 0 or -1;
                if (_boolean.TryGetValue(value, out bool known)) return known;
                if (!active.Add(value) || value.AsgOp is not Operation operation) return false;
                Instruction inst = operation.Inst & Instruction.Mask;
                // Comparisons produce Maxwell predicate values (0/-1), even
                // when the compared operands carry FP32/FP64 type flags.
                bool boolean = inst is Instruction.CompareEqual or Instruction.CompareNotEqual or
                    Instruction.CompareLess or Instruction.CompareLessOrEqual or Instruction.CompareLessU32 or Instruction.CompareLessOrEqualU32 or
                    Instruction.CompareGreater or Instruction.CompareGreaterOrEqual or Instruction.CompareGreaterU32 or Instruction.CompareGreaterOrEqualU32 or
                    Instruction.LogicalAnd or Instruction.LogicalOr or Instruction.LogicalNot or Instruction.LogicalExclusiveOr;
                if (inst is Instruction.BitwiseAnd or Instruction.BitwiseOr or Instruction.BitwiseExclusiveOr or Instruction.BitwiseNot or Instruction.Copy)
                {
                    boolean = true;
                    for (int i = 0; i < operation.SourcesCount; i++)
                        if (!IsBooleanValue(operation.GetSource(i), active)) { boolean = false; break; }
                }
                active.Remove(value); _boolean[value] = boolean; return boolean;
            }

            private Instruction BooleanOperation(Operation operation)
            {
                Instruction inst = operation.Inst & Instruction.Mask;
                if (inst is Instruction.LogicalAnd or Instruction.LogicalOr or Instruction.LogicalNot or Instruction.LogicalExclusiveOr) return inst;
                if (inst is Instruction.BitwiseAnd or Instruction.BitwiseOr or Instruction.BitwiseNot or Instruction.BitwiseExclusiveOr)
                {
                    for (int i = 0; i < operation.SourcesCount; i++)
                        if (!IsBooleanValue(operation.GetSource(i), new HashSet<Operand>())) return 0;
                    return inst switch { Instruction.BitwiseAnd => Instruction.LogicalAnd, Instruction.BitwiseOr => Instruction.LogicalOr,
                        Instruction.BitwiseNot => Instruction.LogicalNot, _ => Instruction.LogicalExclusiveOr };
                }
                return 0;
            }

            private bool TryEvaluateCondition(Operand condition, HashSet<Operand> active, out bool value)
            {
                value = false;
                foreach (var fact in _facts)
                    if (SameStableCondition(fact.condition, condition)) { value = fact.value; return true; }
                if (condition.Type == OperandType.Constant) { value = condition.Value != 0; return true; }
                if (!active.Add(condition) || condition.AsgOp is not Operation operation) return false;
                bool known = false;
                // These are logical operations, not integer bit masks. Two
                // nonzero integers do not imply that their bitwise AND is nonzero.
                Instruction logical = BooleanOperation(operation);
                if (logical is Instruction.LogicalAnd or Instruction.LogicalOr or Instruction.LogicalExclusiveOr)
                {
                    bool ka = TryEvaluateCondition(operation.GetSource(0), active, out bool a);
                    bool kb = TryEvaluateCondition(operation.GetSource(1), active, out bool b);
                    if (logical == Instruction.LogicalAnd)
                    { known = (ka && !a) || (kb && !b) || (ka && kb); value = a && b; }
                    else if (logical == Instruction.LogicalOr)
                    { known = (ka && a) || (kb && b) || (ka && kb); value = a || b; }
                    else { known = ka && kb; value = a != b; }
                }
                else if (logical == Instruction.LogicalNot)
                { known = TryEvaluateCondition(operation.GetSource(0), active, out bool child); value = !child; }
                else if (operation.Inst is Instruction.CompareEqual or Instruction.CompareNotEqual)
                {
                    Operand a = operation.GetSource(0), b = operation.GetSource(1);
                    Operand other = a.Type == OperandType.Constant && a.Value == 0 ? b : b.Type == OperandType.Constant && b.Value == 0 ? a : null;
                    if (other != null)
                    { known = TryEvaluateCondition(other, active, out bool child); value = operation.Inst == Instruction.CompareNotEqual ? child : !child; }
                }
                active.Remove(condition); return known;
            }

            private bool ReachWithout(BasicBlock start, BasicBlock target, BasicBlock avoid, bool afterStart = false)
            {
                HashSet<BasicBlock> seen = new(); Stack<BasicBlock> pending = new();
                if (afterStart)
                {
                    if (start.Next != null && EdgeAllowed(start, start.Next)) pending.Push(start.Next);
                    if (start.Branch != null && EdgeAllowed(start, start.Branch)) pending.Push(start.Branch);
                }
                else pending.Push(start);
                while (pending.TryPop(out BasicBlock current))
                {
                    if (current == avoid || !_reachable.Contains(current) || !seen.Add(current)) continue;
                    if (current == target) return true;
                    if (current.Next != null && EdgeAllowed(current, current.Next)) pending.Push(current.Next);
                    if (current.Branch != null && EdgeAllowed(current, current.Branch)) pending.Push(current.Branch);
                }
                return false;
            }

            private bool SameEpoch(int index, BasicBlock owner)
            {
                if (_epochs.TryGetValue((index, owner), out bool known)) return known;
                BasicBlock anchor = _localFacts[index].anchor;
                bool valid = SameEpochCore(anchor, owner);
                BasicBlock demandOwner = _localFacts[index].demandOwner;
                if (demandOwner != null)
                {
                    // A derived selector truth belongs to the last demanded
                    // Phi evaluation. Both that Phi and the resource Phi must
                    // use the query's same last evaluation of the selector.
                    valid &= SameEpochCore(anchor, demandOwner);
                }
                _epochs[(index, owner)] = valid; return valid;
            }

            private bool SameEpochCore(BasicBlock anchor, BasicBlock owner)
            {
                return owner != anchor && _reachable.Contains(owner) && _reachable.Contains(anchor) &&
                    !ReachWithout(_entry, owner, anchor) && !ReachWithout(_entry, _query, owner) &&
                    !(ReachWithout(owner, anchor, owner, true) && ReachWithout(anchor, _query, owner));
            }

            private bool LocalEdgeAllowed(BasicBlock from, BasicBlock to, int index)
            {
                if (!EdgeAllowed(from, to)) return false;
                var fact = _localFacts[index];
                // Unstable local facts match only this exact SSA condition.
                // Boolean decomposition/structural equivalence belongs solely
                // to globally stable facts, not to another loop evaluation.
                if (from.GetLastOp() is Operation branch && IsConditionalBranch(branch.Inst) && branch.GetSource(0) == fact.condition)
                {
                    bool taken = fact.value == (branch.Inst == Instruction.BranchIfTrue);
                    return to == (taken ? from.Branch : from.Next);
                }
                return true;
            }

            private HashSet<BasicBlock> LocalReachable(int index)
            {
                if (_localReachable.TryGetValue(index, out var known)) return known;
                BasicBlock anchor = _localFacts[index].anchor;
                HashSet<BasicBlock> reachable = new(); Stack<BasicBlock> pending = new(); pending.Push(anchor);
                while (pending.TryPop(out BasicBlock current))
                {
                    if (!_reachable.Contains(current) || !reachable.Add(current)) continue;
                    if (current.Next != null && current.Next != anchor && LocalEdgeAllowed(current, current.Next, index)) pending.Push(current.Next);
                    if (current.Branch != null && current.Branch != anchor && LocalEdgeAllowed(current, current.Branch, index)) pending.Push(current.Branch);
                }
                _localReachable[index] = reachable; return reachable;
            }

            public bool TryGetInputs(PhiNode phi, out List<Operand> inputs)
            {
                inputs = new(); BasicBlock owner = phi.Block;
                if (owner == null || !owner.Operations.Contains(phi) || !_reachable.Contains(owner)) return false;
                // SSA preserves true incoming predecessors separately from its
                // deduplicated definition-block sources. Removed edges may be
                // ignored; every current incoming edge must still have a mapping.
                foreach (BasicBlock predecessor in new HashSet<BasicBlock>(owner.Predecessors))
                {
                    if ((predecessor.Next != owner && predecessor.Branch != owner) ||
                        !phi.TryGetIncomingSource(predecessor, out Operand source)) return false;
                    bool possible = _reachable.Contains(predecessor) && EdgeAllowed(predecessor, owner);
                    for (int i = 0; _useLocalFacts && possible && i < _localFacts.Count; i++)
                        if (SameEpoch(i, owner) && (!LocalReachable(i).Contains(predecessor) || !LocalEdgeAllowed(predecessor, owner, i))) possible = false;
                    if (possible) inputs.Add(source);
                }
                return inputs.Count != 0;
            }

            public bool IsStableValue(Operand value) => IsStable(value, new HashSet<Operand>());
            public bool QueryIsUnreachable => _provedGraph && !_reachable.Contains(_query);

            public bool HasCommonEpoch(HashSet<PhiNode> phis)
            {
                if (!_useLocalFacts) return false;
                for (int i = 0; i < _localFacts.Count; i++)
                {
                    bool valid = true;
                    foreach (PhiNode phi in phis) if (phi.Block == null || !SameEpoch(i, phi.Block)) { valid = false; break; }
                    if (valid) return true;
                }
                return false;
            }
        }

        // Preserve upstream's shallow generic lookup for GlobalToStorage. Its
        // possible-pointer-base enumeration is a different contract from the
        // exact descriptor/epoch proof used by texture and image operations.
        private static bool IsSameAddressCondition(Operand currentCondition, Operand queryCondition)
        {
            if (currentCondition == queryCondition)
            {
                return true;
            }

            return currentCondition.AsgOp is Operation currentOperation &&
                queryCondition.AsgOp is Operation queryOperation &&
                currentOperation.Inst == queryOperation.Inst &&
                AreAllSourcesEqual(currentOperation, queryOperation);
        }

        private static bool BlockConditionsMatch(BasicBlock currentBlock, BasicBlock queryBlock)
        {
            // Check if all the conditions for the query block are satisfied by the current block.
            // Just checks the top-most conditional for now.

            Operation currentBranch = FindBranchSource(currentBlock);
            Operation queryBranch = FindBranchSource(queryBlock);

            Operand currentCondition = currentBranch?.GetSource(0);
            Operand queryCondition = queryBranch?.GetSource(0);

            // The condition should be the same operand instance.

            return currentBranch != null && queryBranch != null &&
                   currentBranch.Inst == queryBranch.Inst &&
                   IsSameAddressCondition(currentCondition, queryCondition);
        }

        public static Operand FindLastOperation(Operand source, BasicBlock block, bool recurse = true)
        {
            if (source.AsgOp is PhiNode phiNode)
            {
                int undefCount = 0;
                for (int i = phiNode.SourcesCount - 1; i >= 0; i--)
                {
                    BasicBlock phiBlock = phiNode.GetBlock(i);
                    Operand phiSource = phiNode.GetSource(i);
                    if (BlockConditionsMatch(block, phiBlock)) return phiSource;
                    else if (recurse && phiSource.AsgOp is PhiNode)
                    {
                        Operand match = FindLastOperation(phiSource, block, false);
                        if (match != phiSource) return match;
                    }
                    else if (phiSource.Type == OperandType.Undefined) undefCount++;
                }
                if (undefCount == phiNode.SourcesCount - 1)
                    for (int i = phiNode.SourcesCount - 1; i >= 0; i--)
                    {
                        Operand phiSource = phiNode.GetSource(i);
                        if (phiSource.Type != OperandType.Undefined) return phiSource;
                    }
            }
            return source;
        }

        // Only bindless resource lowering uses this exact descriptor proof.
        // Do not use its constant/handle spill results as pointer provenance.
        public static Operand FindResourceHandleOperation(Operand source, BasicBlock block, bool recurse = true)
        {
            return FindResourceHandleOperation(source, block, recurse, new HashSet<Operand>(), true);
        }

        public static bool IsBlockProvablyUnreachable(BasicBlock block) => new GuardedPhiContext(block).QueryIsUnreachable;

        private static Operand FindResourceHandleOperation(Operand source, BasicBlock block, bool recurse, HashSet<Operand> expanding, bool useLocalFacts)
        {
            if (TryForwardStaticLocalHandle(source, block, out Operand stored)) return stored;
            if (source.AsgOp is not PhiNode) return source;
            if (!expanding.Add(source)) return source;
            try
            {
            GuardedPhiContext context = new(block, useLocalFacts);
            HashSet<PhiNode> visited = new(); Stack<Operand> pending = new(); pending.Push(source);
            Operand result = null;
            while (pending.TryPop(out Operand current))
            {
                if (TryForwardStaticLocalHandle(current, block, out stored)) current = stored;
                if (current.Type == OperandType.Undefined) continue;
                if (current.AsgOp is PhiNode phiNode && (recurse || current == source))
                {
                    if (!visited.Add(phiNode)) continue;
                    if (!context.TryGetInputs(phiNode, out List<Operand> inputs))
                    {
                        if (phiNode.Block != null) return source;
                        inputs = new();
                        for (int i = 0; i < phiNode.SourcesCount; i++)
                            if (phiNode.GetSource(i).Type != OperandType.Undefined) inputs.Add(phiNode.GetSource(i));
                        if (inputs.Count != 1) return source;
                    }
                    foreach (Operand input in inputs) pending.Push(input);
                }
                else
                {
                    // Phi(A,self) has the fixed value A only with an external,
                    // stable seed. A loop sample/load may denote another iteration
                    // even when its instruction/SSA object is identical.
                    if (result == null) result = current;
                    else if (!IsSameOperand(result, current)) return source;
                }
            }
            if (result == null) return source;
            if (!context.IsStableValue(result))
            {
                // Only a finite, proven cbuf handle combination may use a local
                // iteration fact. Raw sampled/storage handles remain unresolved.
                if (result.AsgOp is not Operation operation ||
                    operation.Inst != Instruction.BitwiseOr || operation.SourcesCount != 2) return source;
                bool componentLocalFacts = context.HasCommonEpoch(visited);
                for (int i = 0; i < 2; i++)
                {
                    // Without a common current-iteration epoch, the OR may
                    // denote an earlier loop evaluation. Its components must
                    // then resolve with global invariant facts only.
                    Operand component = FindResourceHandleOperation(operation.GetSource(i), block, true, expanding, componentLocalFacts);
                    if (component.Type is not (OperandType.ConstantBuffer or OperandType.Constant)) return source;
                }
            }
            return result;
            }
            finally { expanding.Remove(source); }
        }

        private static bool TryForwardStaticLocalHandle(Operand source, BasicBlock block, out Operand value)
        {
            value = null;
            if (source.AsgOp is not Operation load || load.Inst != Instruction.Load ||
                load.StorageKind != StorageKind.LocalMemory || load.DestsCount != 1 || load.Index != 0 ||
                load.SourcesCount != 2 || load.GetSource(0).Type != OperandType.Constant ||
                load.GetSource(1).Type != OperandType.Constant) return false;
            LinkedListNode<INode> node = block.Operations.Find(load);
            if (node == null) return false;
            for (node = node.Previous; node != null; node = node.Previous)
            {
                if (node.Value is not Operation operation) continue;
                if (operation.Inst == Instruction.Call) return false;
                if (operation.StorageKind != StorageKind.LocalMemory || operation.Inst == Instruction.Load) continue;
                if (operation.Inst != Instruction.Store || operation.SourcesCount != 3 || operation.DestsCount != 0 ||
                    operation.GetSource(0).Type != OperandType.Constant) return false;
                if (operation.GetSource(0).Value != load.GetSource(0).Value) continue;
                // A dynamic address may alias this word. Do not search through
                // it or through an unknown mutation of the private local array.
                if (operation.GetSource(1).Type != OperandType.Constant) return false;
                if (operation.GetSource(1).Value != load.GetSource(1).Value) continue;
                Operand candidate = operation.GetSource(2);
                bool IsConstant(Operand operand) => operand.Type is OperandType.Constant or OperandType.ConstantBuffer;
                if (IsConstant(candidate) || candidate.AsgOp is Operation combine &&
                    combine.Inst == Instruction.BitwiseOr && combine.Index == 0 && combine.DestsCount == 1 &&
                    combine.SourcesCount == 2 && IsConstant(combine.GetSource(0)) && IsConstant(combine.GetSource(1)))
                {
                    // Restrict forwarding to static descriptor components. A
                    // sampled/mutable SSA value could refer to an older loop
                    // evaluation and needs a separate resource lifetime proof.
                    value = candidate; return true;
                }
                return false;
            }
            return false;
        }

        public static void DeleteNode(LinkedListNode<INode> node, Operation operation)
        {
            node.List.Remove(node);

            for (int srcIndex = 0; srcIndex < operation.SourcesCount; srcIndex++)
            {
                operation.SetSource(srcIndex, null);
            }

            operation.Dest = null;
        }
    }
}
