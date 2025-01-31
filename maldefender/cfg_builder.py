#!/usr/bin/python3.7
import re
import os
import glog as log
import networkx as nx
import instructions as isn
import numpy as np
import matplotlib.pyplot as plt
from dp_utils import FakeCalleeAddr, addCodeSegLog, InvalidAddr
from collections import OrderedDict
from typing import List, Dict, Set


class Block(object):
    """Block of control flow graph."""
    # Feature index: 11, 12, 13
    oneGram = ['00', 'FF', '??']
    oneGram2Idx = {k: v for (v, k) in enumerate(oneGram)}
    # Feature index: 14 - 23
    fourGram = ['????????', '04000000', '5DC30000', 'F0F0F001', '00100000',
                '00F0F000', '0D2F0600', '5DC38BFF', '8BFF558B', '840D2F06']
    fourGram2Idx = {k: v for (v, k) in enumerate(fourGram)}
    instDim = len(isn.Instruction.operandTypes) + \
        len(isn.Instruction.operatorTypes) + \
        len(oneGram) + len(fourGram) + len(isn.Instruction.specialChars)

    """Types of structual-related vertex features"""
    vertexTypes = {'degree': instDim, 'num_inst': instDim + 1}

    def __init__(self) -> None:
        super(Block, self).__init__()
        self.startAddr = -1
        self.endAddr = -1
        self.instList: List[isn.Instruction] = []
        self.edgeList: List[int] = []

    def bytesFromInsts(self) -> List[str]:
        byteList = []
        for inst in self.instList:
            for byte in inst.bytes:
                byte = byte.rstrip('\n+')
                byteList.append(byte)

        return byteList

    def get1gramFeatures(self) -> List[int]:
        features = [0] * len(Block.oneGram)
        for byte in self.bytesFromInsts():
            byte = byte.rstrip('\n+')
            if byte in Block.oneGram2Idx:
                features[Block.oneGram2Idx[byte]] += 1

        return features

    def get4gramFeatures(self) -> List[int]:
        def check4Gram():
            windowStr = ''.join(window)
            if windowStr in Block.fourGram2Idx:
                features[Block.fourGram2Idx[windowStr]] += 1

        features = [0] * len(Block.fourGram)
        window = []
        for byte in self.bytesFromInsts():
            if len(window) == 4:
                check4Gram()
                window.pop(0)

            window.append(byte)

        if len(window) == 4:
            check4Gram()

        return features

    def getAttributes(self):
        instAttr = np.zeros((1, Block.instDim))
        added = False
        for inst in self.instList:
            attr = inst.getOperandFeatures()
            attr += inst.getOperatorFeatures()

            if added is False:
                attr += self.get1gramFeatures()
                attr += self.get4gramFeatures()
                added = True
            else:
                attr += [0] * (len(Block.oneGram) + len(Block.fourGram))

            attr += inst.getSpecialCharFeatures()
            instAttr += np.array(attr)

        degree = len(self.edgeList)
        numInst = len(self.instList)
        return np.concatenate((instAttr, [degree, numInst]), axis=None)

    @staticmethod
    def getAttributesDim():
        return Block.instDim + len(Block.vertexTypes)


class ControlFlowGraphBuilder(object):
    """For building a control flow graph from a program"""

    def __init__(self, binaryId: str, pathPrefix: str) -> None:
        super(ControlFlowGraphBuilder, self).__init__()
        self.cfg = nx.DiGraph()
        self.instBuilder: isn.InstBuilder = isn.InstBuilder()
        self.binaryId: str = binaryId
        self.filePrefix: str = pathPrefix + '/' + binaryId
        self.programEnd: int = -1
        self.programStart: int = -1

        self.text: Dict[int, str] = {}  # Line number to raw string instruction
        self.program: Dict[str, str] = {}  # Addr to raw string instruction
        self.addr2Inst: OrderedDict[int, isn.Instruction] = OrderedDict()
        self.addr2InstAux: OrderedDict[int, isn.Instruction] = OrderedDict()
        self.addr2Bytes: OrderedDict[int, List[str]] = OrderedDict()
        self.addr2RawStr: OrderedDict[int, List[str]] = OrderedDict()
        self.addr2Block: Dict[int, Block] = {}

    def getControlFlowGraph(self) -> nx.DiGraph:
        self.buildControlFlowGraph()
        self.exportToNxGraph()
        return self.cfg

    def buildControlFlowGraph(self) -> None:
        self.parseInstructions()
        self.parseBlocks()

    def parseInstructions(self) -> Set[str]:
        """First pass on instructions"""
        self.extractTextSeg()
        self.createProgram()
        self.buildInsts()
        return self.instBuilder.seenInst
        # self.clearTmpFiles()

    def parseBlocks(self) -> None:
        """Second pass on blocks"""
        self.visitInsts()
        self.connectBlocks()

    def addrInCodeSegment(self, seg: str) -> str:
        segNames = [
            '.text:', 'CODE:', 'UPX1:', 'seg000:', 'qmoyiu:',
            '.UfPOkc:', '.brick:', '.icode:', 'seg001:',
            '.Much:', 'iuagwws:', '.idata:', '.edata:',
            '.IqR:', '.data:', '.bss:', '.idata:', '.rsrc:',
            '.tls:', '.reloc:', '.unpack:', '_1:', '.Upack:', '.mF:']
        for prefix in segNames:
            if seg.startswith(prefix) is True:
                colonIdx = seg.rfind(':')
                if colonIdx != -1:
                    return seg[colonIdx + 1:]
                else:
                    return seg[-8:]

        return "NotInCodeSeg"

    def appendRawBytes(self, addrStr: str, byte: str):
        addr = int(addrStr, 16)
        if addr in self.addr2Bytes:
            self.addr2Bytes[addr].append(byte)
        else:
            self.addr2Bytes[addr] = [byte]

    def indexOfInst(self, decodedElems: List[str], addrStr: str) -> int:
        idx = 0
        bytePattern = re.compile(r'^[A-F0-9?][A-F0-9?]\+?$')
        while idx < len(decodedElems) and bytePattern.match(decodedElems[idx]):
            self.appendRawBytes(addrStr, decodedElems[idx])
            idx += 1

        return idx

    def indexOfComment(self, decodedElems: List[str]) -> int:
        for (i, elem) in enumerate(decodedElems):
            if elem.find(';') != -1:
                return i

        return len(decodedElems)

    def extractTextSeg(self) -> None:
        """Extract text segment from .asm file"""
        log.debug(f'[ExtractSeg] Extracting {self.binaryId}.asm ****')
        lineNum = 1
        imcompleteByte = re.compile(r'^\?\?$')
        fileInput = open(self.filePrefix + '.asm', 'rb')
        for line in fileInput:
            elems = line.split()
            decodedElems = [x.decode("utf-8", "ignore") for x in elems]
            if len(decodedElems) == 0:
                lineNum += 1
                continue

            seg = decodedElems.pop(0)
            addr = self.addrInCodeSegment(seg)
            if addr is "NotInCodeSeg":
                # Since text segment maynot always be the head, we cannot break
                log.debug(f"[ExtractSeg] Line {lineNum} out of text segment")
                lineNum += 1
                continue

            startIdx = self.indexOfInst(decodedElems, addr)
            endIdx = self.indexOfComment(decodedElems)
            if startIdx < endIdx:
                instElems = [addr] + decodedElems[startIdx: endIdx]
                s1, s2 = ' '.join(decodedElems), ' '.join(instElems)
                log.debug(f"[ExtractSeg] Processed L{lineNum}: '{s1}'=>'{s2}'")
                self.text[lineNum] = " ".join(instElems)
            else:
                l = " ".join(decodedElems)
                log.debug(f'[ExtractSeg] No instruction L{lineNum}: {l}')

            lineNum += 1

        fileInput.close()

    def isHeaderInfo(self, sameAddrInsts: List[str]) -> bool:
        for inst in sameAddrInsts:
            if inst.startswith('_text segment') or inst.find('.mmx') != -1:
                return True

        return False

    def appendRawString(self, addr: int, instRawStrs: List[str]):
        for inst in instRawStrs:
            if addr in self.addr2RawStr:
                self.addr2RawStr[addr].append(inst)
            else:
                self.addr2RawStr[addr] = [inst]

    def aggregate(self, addrStr: str, sameAddrInsts: List[str]) -> None:
        """
        Case 1: Header info
        Case 2: 'xxxxxx proc near' => keep last inst
        Case 3: 'xxxxxx endp' => ignore second
        Case 4: dd, db, dw instructions => d? var_name
        Case 5: location label followed by regular inst
        Case 6: Just 1 regular inst
        """
        addr = int(addrStr, 16)
        self.appendRawString(addr, sameAddrInsts)

        if self.isHeaderInfo(sameAddrInsts):
            self.program[addrStr] = sameAddrInsts[-1]
            return

        validInst: List[str] = []
        foundDataDeclare: str = ''
        ptrPattern = re.compile(r'.+=.+ ptr .+')
        for inst in sameAddrInsts:
            if inst.find('proc near') != -1 or inst.find('proc far') != -1:
                continue
            if inst.find('public') != -1:
                continue
            if inst.find('assume') != -1:
                continue
            if inst.find('endp') != -1 or inst.find('ends') != -1:
                continue
            if inst.find(' = ') != -1 or ptrPattern.match(inst):
                log.debug(f'Ptr declare found: {inst}')
                foundDataDeclare += inst + ' '
                continue
            if inst.startswith('dw ') or inst.find(' dw ') != -1:
                foundDataDeclare += inst + ' '
                continue
            if inst.startswith('dd ') or inst.find(' dd ') != -1:
                foundDataDeclare += inst + ' '
                continue
            if inst.startswith('db ') or inst.find(' db ') != -1:
                foundDataDeclare += inst + ' '
                continue
            if inst.startswith('dt ') or inst.find(' dt ') != -1:
                foundDataDeclare += inst + ' '
                continue
            if inst.startswith('unicode '):
                foundDataDeclare += inst + ' '
                continue
            if inst.endswith(':'):
                continue

            validInst.append(inst)

        if len(validInst) == 1:
            progLine = validInst[0] + ' ' + foundDataDeclare
            self.program[addrStr] = progLine.rstrip(' ')
            log.debug(f'[AggrInst] Aggregate succeed')
        elif len(foundDataDeclare.rstrip(' ')) > 0:
            self.program[addrStr] = foundDataDeclare.rstrip(' ')
            log.debug(f'[AggrInst] Concat all DataDef into unified inst')
        else:
            # Concat unaggregatable insts
            log.debug(f'[AggrInst:{self.binaryId}] Fail aggregating insts at {addrStr}')
            progLine = ''
            for inst in validInst:
                progLine += inst.rstrip('\n\\') + ' '
                log.debug('[AggrInst] %s: %s' % (addrStr, inst))

            log.debug(f'[AggrInst:{self.binaryId}] Concat to: {progLine}')
            self.program[addrStr] = progLine.rstrip(' ')

    def createProgram(self) -> None:
        """Generate unique-addressed program, store in self.program"""
        log.debug('[CreateProg] Aggreate to unique-addressed instructions')
        currAddr = -1
        sameAddrInsts = []
        for (lineNum, line) in self.text.items():
            elems = line.split(' ')
            addr, inst = elems[0], elems[1:]
            if currAddr == -1:
                currAddr = addr
                sameAddrInsts.append(" ".join(inst))
            else:
                if addr != currAddr:
                    self.aggregate(currAddr, sameAddrInsts)
                    sameAddrInsts.clear()

                currAddr = addr
                sameAddrInsts.append(" ".join(inst))

        if len(sameAddrInsts) > 0:
            self.aggregate(currAddr, sameAddrInsts)

        if len(self.program) == 0:
            log.debug(f'[CreateProg] No code in {self.filePrefix}.asm')

    def buildInsts(self) -> None:
        """Create Instruction object for each address, store in addr2Inst"""
        log.debug(f'[BuildInsts] Build insts from {self.filePrefix + ".prog"}')
        prevAddr = -1
        for (addr, line) in self.program.items():
            inst = self.instBuilder.createInst(addr + ' ' + line)
            if inst is None:
                continue

            if prevAddr != -1:
                self.addr2Inst[prevAddr].size = inst.address - prevAddr

            self.addr2Inst[inst.address] = inst
            log.debug(f'{inst.address:x} {inst.operand}')
            if self.programStart == -1:
                self.programStart = inst.address
            self.programEnd = max(inst.address, self.programEnd)
            prevAddr = inst.address

        # Last inst get default size 2
        if prevAddr > 0:
            self.addr2Inst[prevAddr].size = 2
        else:
            addCodeSegLog(self.binaryId)

        start, end = self.programStart, self.programEnd
        log.debug(f'[BuildInsts] Finish with range [{start:x}, {end:x}]')

    def visitInsts(self) -> None:
        log.debug(f'[VisitInsts] Visiting insts in {self.binaryId}')
        for addr, inst in self.addr2Inst.items():
            inst.accept(self)
            if addr in self.addr2Bytes:
                inst.bytes = self.addr2Bytes[addr]
            else:
                log.warning(f'{addr:X} don\'t have any bytes')

            if addr in self.addr2RawStr:
                inst.rawStrs = self.addr2RawStr[addr]
            else:
                log.warning(f'{addr:X} don\'t have any raw string')

        self.addr2Inst.update(self.addr2InstAux)

    def addAuxilaryInst(self, addr, operandName='') -> None:
        if addr not in self.addr2InstAux:
            self.addr2InstAux[addr] = isn.Instruction(addr, operand=operandName)
            self.addr2InstAux[addr].start = True
            self.addr2InstAux[addr].fallThrough = False

    def enter(self, inst, enterAddr: int) -> None:
        if enterAddr == FakeCalleeAddr:
            log.debug(f'[Enter] extern callee addr from {inst}')
            self.addAuxilaryInst(enterAddr, 'extrn_sym')
        elif enterAddr >= 0 and enterAddr < 256:
            log.debug(f'[Enter] software interrupt {enterAddr:x}')
            self.addAuxilaryInst(enterAddr, 'softirq_%X' % enterAddr)
        elif enterAddr not in self.addr2Inst:
            if inst.operand in ['call', 'syscall']:
                log.debug(f'[Enter] extern callee addr from {inst}')
                self.addAuxilaryInst(enterAddr, 'extrn_sym')
            else:
                log.debug(f'[Enter] invalid address {enterAddr:x} from {inst}')
                self.addAuxilaryInst(InvalidAddr, 'invalid')
                self.addr2Inst[inst.address].branchTo = InvalidAddr
        else:
            log.debug(f'[Enter] instruction at {enterAddr:x} from {inst}')
            self.addr2Inst[enterAddr].start = True

    def branch(self, inst) -> None:
        """Conditional jump to another address or fall throught"""
        branchToAddr = inst.findAddrInInst()
        self.addr2Inst[inst.address].branchTo = branchToAddr
        log.debug(f'[Branch] From {inst.address:x} to {branchToAddr:x}')
        self.enter(inst, branchToAddr)
        self.enter(inst, inst.address + inst.size)

    def call(self, inst) -> None:
        """Jump out and then back"""
        self.addr2Inst[inst.address].call = True
        # Likely NOT able to find callee's address (e.g. extern symbols)
        callAddr = inst.findAddrInInst()
        if callAddr != FakeCalleeAddr:
            log.debug(f'[Call] Found from {inst.address:x} to {callAddr:x}')
        else:
            log.debug(f'[Call] Fake from {inst.address:x} to FakeCalleeAddr')

        self.addr2Inst[inst.address].branchTo = callAddr
        self.enter(inst, callAddr)

    def jump(self, inst) -> None:
        """Unconditional jump to another address"""
        jumpAddr = inst.findAddrInInst()
        self.addr2Inst[inst.address].fallThrough = False
        self.addr2Inst[inst.address].branchTo = jumpAddr
        log.debug(f'[Jump] from {inst.address:x} to {jumpAddr:x}')
        self.enter(inst, jumpAddr)
        self.enter(inst, inst.address + inst.size)

    def end(self, inst) -> None:
        """Stop fall throught"""
        self.addr2Inst[inst.address].fallThrough = False
        log.debug(f'[End] at {inst.address:x}')
        if inst.address + inst.size <= self.programEnd:
            self.enter(inst, inst.address + inst.size)

    def visitDefault(self, inst) -> None:
        pass

    def visitCalling(self, inst) -> None:
        self.call(inst)

    def visitConditionalJump(self, inst) -> None:
        self.branch(inst)

    def visitUnconditionalJump(self, inst) -> None:
        self.jump(inst)

    def visitEndHere(self, inst) -> None:
        self.end(inst)

    def getBlockAtAddr(self, addr: int) -> Block:
        if addr not in self.addr2Block:
            block = Block()
            block.startAddr = addr
            block.endAddr = addr
            self.addr2Block[addr] = block
            log.debug(f'Create new block starting/ending at {addr:x}')

        return self.addr2Block[addr]

    def connectBlocks(self) -> None:
        """
        Group instructions into blocks, and
        connected based on branch and fall through.
        """
        log.debug('[ConnectBlocks] Create and connect blocks')
        currBlock = None
        for (addr, inst) in sorted(self.addr2Inst.items()):
            if currBlock is None or inst.start is True:
                currBlock = self.getBlockAtAddr(addr)
            nextAddr = addr + inst.size
            nextBlock = currBlock
            if nextAddr in self.addr2Inst:
                nextInst = self.addr2Inst[nextAddr]
                if inst.fallThrough is True and nextInst.start is True:
                    nextBlock = self.getBlockAtAddr(nextAddr)
                    currBlock.edgeList.append(nextBlock.startAddr)
                    addr1, addr2 = currBlock.startAddr, nextBlock.startAddr
                    log.debug(f'[ConnectBlocks] B{addr1:x} falls to B{addr2:x}')

            if inst.branchTo is not None:
                block = self.getBlockAtAddr(inst.branchTo)
                if block.startAddr not in currBlock.edgeList:
                    currBlock.edgeList.append(block.startAddr)

                addr1, addr2 = currBlock.startAddr, block.startAddr
                log.debug(f'[ConnectBlocks] B{addr1:x} branches to B{addr2:x}')
                if inst.call is True:
                    if currBlock.startAddr not in block.edgeList:
                        block.edgeList.append(currBlock.startAddr)

                    log.debug(f'[ConnectBlocks] B{addr2:x} ret to B{addr1:x}')

            currBlock.instList.append(inst)
            currBlock.endAddr = max(currBlock.endAddr, inst.address)
            self.addr2Block[currBlock.startAddr] = currBlock
            currBlock = nextBlock

    def exportToNxGraph(self):
        """Assume block/node is represented by its startAddr"""
        log.debug('[ExportToNxGraph] Generate DiGraph from connected blocks')
        for (addr, block) in sorted(self.addr2Block.items()):
            self.cfg.add_node(addr, block=block)

        for (addr, block) in self.addr2Block.items():
            for neighboor in block.edgeList:
                self.cfg.add_edge(addr, neighboor)

    def drawCfg(self) -> None:
        log.debug(f'[DrawCfg] Save graph plot to {self.filePrefix}.pdf')
        nx.draw(self.cfg, with_labels=True, font_weight='normal')
        plt.savefig('%s.pdf' % self.filePrefix, format='pdf')
        plt.clf()

    def printCfg(self):
        log.debug(f'[PrintCfg] Print = {nx.number_of_nodes(self.cfg)} nodes')
        for (addr, block) in sorted(self.addr2Block.items()):
            start, end = block.startAddr, block.endAddr
            log.debug(f'[PrintCfg] Block {addr:x}: [{start:x}, {end:x}]')

        log.debug(f'[PrintCfg] Print {nx.number_of_edges(self.cfg)} edges')
        for (addr, block) in sorted(self.addr2Block.items()):
            for neighboor in block.edgeList:
                log.debug(f'[PrintCfg] Edge {addr:x} -> {neighboor:x}')

        self.drawCfg()

    def saveProgram(self) -> None:
        progFile = open(self.filePrefix + '.prog', 'w')
        for (addr, inst) in self.program.items():
            progFile.write(addr + ' ' + inst + '\n')

        progFile.close()

    def saveText(self) -> None:
        textFile = open(self.filePrefix + '.text', 'w')
        for (lineNum, line) in self.text.items():
            textFile.write(lineNum + ' ' + inst + '\n')

        textFile.close()

    def clearTmpFiles(self) -> None:
        log.debug('[ClearTmpFiles] Remove temporary files')
        for ext in ['.text', '.prog']:
            os.remove(self.filePrefix + ext)


class AcfgBuilder(object):
    def __init__(self, binaryId: str, pathPrefix: str) -> None:
        super(AcfgBuilder, self).__init__()
        self.cfgBuilder = ControlFlowGraphBuilder(binaryId, pathPrefix)
        self.cfg: nx.DiGraph = None

    def extractBlockAttributes(self):
        """
        Extract features in each block.
        """
        log.debug('[ExtractBlockAttrs] Extract block attributes from CFG')
        features = np.zeros((self.cfg.number_of_nodes(),
                             Block.getAttributesDim()), dtype=float)
        for (i, (node, attributes)) in enumerate(sorted(self.cfg.nodes(data=True))):
            block = attributes['block']
            log.debug(f'[ExtractBlockAttrs] Extracting B{block.startAddr:x}')
            features[i, :] = block.getAttributes()

        return features

    def getAttributedCfg(self):
        self.cfg = self.cfgBuilder.getControlFlowGraph()
        if self.cfg.number_of_nodes() == 0:
            return [None, None]

        blockAttrs = self.extractBlockAttributes()
        adjMatrix = nx.adjacency_matrix(self.cfg,
                                        nodelist=sorted(self.cfg.nodes()))
        return [blockAttrs, adjMatrix]
