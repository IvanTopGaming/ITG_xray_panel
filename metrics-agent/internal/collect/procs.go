package collect

import (
	"bufio"
	"io/fs"
	"sort"
	"strconv"
	"strings"
)

type Procs struct {
	Root     fs.FS
	PageSize int64
}

type ProcInfo struct {
	PID        int
	Comm       string
	CPUJiffies int64
	RSSBytes   int64
}

func (p *Procs) Snapshot(n int) ([]ProcInfo, error) {
	entries, err := fs.ReadDir(p.Root, ".")
	if err != nil {
		return nil, err
	}
	var out []ProcInfo
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		pid, err := strconv.Atoi(e.Name())
		if err != nil {
			continue
		}
		info := ProcInfo{PID: pid}
		info.Comm = p.firstLine(e.Name() + "/comm")
		info.CPUJiffies = p.cpuJiffies(e.Name() + "/stat")
		info.RSSBytes = p.rss(e.Name() + "/statm")
		out = append(out, info)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].CPUJiffies > out[j].CPUJiffies })
	if len(out) > n {
		out = out[:n]
	}
	return out, nil
}

func (p *Procs) firstLine(name string) string {
	f, err := p.Root.Open(name)
	if err != nil {
		return ""
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	if sc.Scan() {
		return strings.TrimSpace(sc.Text())
	}
	return ""
}

func (p *Procs) cpuJiffies(name string) int64 {
	line := p.firstLine(name)
	if line == "" {
		return 0
	}
	end := strings.LastIndexByte(line, ')')
	if end < 0 || end+2 >= len(line) {
		return 0
	}
	fields := strings.Fields(line[end+2:])
	if len(fields) < 13 {
		return 0
	}
	ut, _ := strconv.ParseInt(fields[11], 10, 64)
	st, _ := strconv.ParseInt(fields[12], 10, 64)
	return ut + st
}

func (p *Procs) rss(name string) int64 {
	line := p.firstLine(name)
	f := strings.Fields(line)
	if len(f) < 2 {
		return 0
	}
	pages, _ := strconv.ParseInt(f[1], 10, 64)
	return pages * p.PageSize
}
