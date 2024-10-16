
import itertools
from io import StringIO as BytesIO
binary_type = str

from compileengine.variable import Variable


class VariableCollection(object):
    def __init__(self, engine, inst_class):
        object.__setattr__(self, '_cache', {})
        object.__setattr__(self, 'engine', engine)
        object.__setattr__(self, '_inst_class', inst_class)

    def _create(self, name):
        var = self._inst_class()
        var.name = name
        var.engine = self.engine
        return var

    def __getattr__(self, name):
        try:
            return self._cache[name]
        except KeyError:
            var = self._create(name)
            self._cache[name] = var
            return var

    def __setattr__(self, name, value):
        var = getattr(self, name)
        var.value = value

    def __dir__(self):
        return self._cache.keys()


class Function(Variable):
    """Function

    This variable type is callable
    """
    def __call__(self, *args):
        # self.engine.func(self, *args)
        return


class FunctionCollection(VariableCollection):
    def __setattr__(self, name, value):
        raise TypeError('Cannot set a function')


class NewBranch(Exception):
    pass


class EngineBlock(object):
    """Stored compiled block

    Attributes
    ----------
    engine : Engine
        Reference to parent engine
    buff : str
        Value of block
    jumps : dict
        Map of offset (relative to block start) to another block
    offset : int, optional
        Determined offset of block
    """
    def __init__(self, engine):
        self.engine = engine
        self.buff = None
        self.jumps = {}
        self.offset = -1

    def __eq__(self, other):
        if self is other:
            return True
        if len(self.buff) != len(other.buff):
            return False
        idx = 0
        while idx < len(self.buff):
            if idx in self.jumps:
                if idx not in other.jumps:
                    return False
                if self.jumps[idx] != other.jumps[idx]:
                    return False
                idx += self.engine.pointer_size
                continue
            byte1 = self.buff[idx]
            byte2 = other.buff[idx]
            if byte1 != byte2:
                return False
            idx += 1
        return True

    def __ne__(self, other):
        return not self.__eq__(other)


class Engine(BytesIO):
    """Execute a decompiled function with this object to compile it
    """
    variable_collection_class = VariableCollection
    function_collection_class = FunctionCollection
    variable_class = Variable
    function_class = Function
    pointer_size = 4

    STATE_IDLE = 0
    STATE_BUILDING_BRANCHES = 1
    STATE_COMPILING = 2

    def __init__(self):
        BytesIO.__init__(self)
        self.vars = self._init_vars()
        self.funcs = self._init_funcs()
        self.state = self.STATE_IDLE
        self.stack = []
        self.current_block = None
        self.state_blocks = {}
        self.path_stack = []
        self.blocks = []

    def write_value(self, value, size=4):
        """Write a fixed length value to the buffer

        Parameters
        ----------
        value : int
            Unsigned value to write
        size : int
            Number of bytes that value should occupy
        """
        buff = binary_type()
        for i in range(size):
            buff += chr((value >> (i*8)) & 0xFF)
        self.write(buff)

    def reset(self):
        self.truncate(0)
        self.seek(0)

    def push(self, state=None):
        block = self.current_block
        block.buff = self.getvalue()
        self.truncate(0)
        self.seek(0)
        self.stack.append(block)
        if state is None:
            state = object()
        self.path_stack.append(state)
        try:
            self.current_block = self.state_blocks[tuple(self.path_stack)]
        except KeyError:
            self.current_block = EngineBlock(self)
            self.blocks.append(self.current_block)
            self.state_blocks[tuple(self.path_stack)] = self.current_block

    def pop(self):
        self.current_block.buff = self.getvalue()
        block = self.stack.pop()
        self.path_stack.pop()
        self.current_block = block
        self.truncate(0)
        self.seek(0)
        self.write(block.buff)

    def _init_vars(self):
        return self.variable_collection_class(self, self.variable_class)

    def _init_funcs(self):
        return self.function_collection_class(self, self.function_class)

    def write_end(self, value):
        return

    def compile(self, func):
        try:
            self.state = self.STATE_BUILDING_BRANCHES
            self._find_branches(func)
            self.state = self.STATE_COMPILING
            self.state_blocks = {}
            self.current_block = script_block = EngineBlock(self)
            self.blocks.append(self.current_block)
            for path in self.paths:
                self.truncate(0)
                self.seek(0)
                self.branch_id = 0
                self.current_path = path
                ret = func(self)
                self.write_end(ret)
                self.current_block.buff = self.getvalue()
                while self.stack:
                    self.pop()
            return script_block
        finally:
            self.state = self.STATE_IDLE

    def _find_branches(self, func):
        self.paths = [()]
        self.loops = []
        self.path_id = 0
        while self.path_id < len(self.paths):
            self.current_path = self.paths[self.path_id]
            self.branch_id = 0
            try:
                func(self)
            except NewBranch:
                self.paths[self.path_id] = self.current_path+(NewBranch,)
            self.path_id += 1
        self.paths = [path for path in self.paths
                      if path[-1:] != (NewBranch, )]
        self.paths.sort(reverse=True)

    def write_branch(self, branch_state, condition):
        ofs = self.tell()
        self.write_value(0, self.pointer_size)
        return ofs

    def write_jump(self):
        self.write_value(0, self.pointer_size)
        return self.tell()-self.pointer_size

    def branch(self, condition):
        try:
            value = self.current_path[self.branch_id]
            self.branch_id += 1
        except IndexError:
            self.paths.append(self.current_path+(True, ))
            self.paths.append(self.current_path+(False, ))
            raise NewBranch
        if self.state == self.STATE_COMPILING:
            old_block = self.current_block
            true_ofs = self.write_branch(True, condition)
            false_ofs = self.write_branch(False, condition)
            self.push(value)
            # Write two jumps back to back. True then False
            # Only set the jump for the active branch though
            if value is True:
                old_block.jumps[true_ofs] = self.current_block
            if value is False:
                old_block.jumps[false_ofs] = self.current_block
        return value

    def loop(self, condition):
        raise NotImplementedError('Loops are not currently handled')
        return False

    def call(self, new_func):
        if self.state == self.STATE_COMPILING:
            block = self.current_block
            ofs = self.write_jump()
            self.push(new_func)
            block.jumps[ofs] = self.current_block
        ret = new_func(self)
        if self.state == self.STATE_COMPILING:
            self.write_end(ret)
            self.pop()
        return ret

    def unknown(self, value, size):
        if self.state == self.STATE_COMPILING:
            self.write_value(value, size)
        return '1+1'
