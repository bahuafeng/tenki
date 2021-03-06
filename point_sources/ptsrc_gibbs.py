import numpy as np, argparse, sys, itertools, os, errno, warnings
from mpi4py import MPI
from enlib import enmap as en, powspec, utils
from enlib.degrees_of_freedom import DOF, Arg
from enlib.cg import CG
warnings.filterwarnings("ignore")

#from matplotlib.pylab import *
parser = argparse.ArgumentParser()
parser.add_argument("freqs")
parser.add_argument("maps")
parser.add_argument("noise")
parser.add_argument("powspec")
parser.add_argument("posfile")
parser.add_argument("odir")
parser.add_argument("-R", "--radius", type=float, default=30)
parser.add_argument("--burnin", type=int, default=10)
parser.add_argument("-n", "--nsamp", type=int, default=50)
parser.add_argument("--dump", type=int, default=0)
parser.add_argument("-v", "--verbose", action="store_true")
parser.add_argument("-i", type=int, default=0)
parser.add_argument("--nmax", type=int, default=0)
parser.add_argument("--mindist-group", type=float, default=10)
parser.add_argument("-c", "--cont", action="store_true")
args = parser.parse_args()

comm = MPI.COMM_WORLD
myid = comm.rank
nproc= comm.size
r2c = 180/np.pi
r2b = r2c*60*(8*np.log(2))**0.5

def read_maps(fmt, n, ntot=4):
	try:
		maps = en.read_map(fmt)
		if maps.ndim == ntot-1: maps = en.enmap([maps]*n,maps.wcs)
		if maps.ndim != ntot: raise ValueError("Map %s must have %d dimensions" % (fmt,ntot))
		return maps
	except IOError:
		maps = [en.read_map(fmt % i) for i in range(n)]
		maps = en.ndmap(maps, maps[0].wcs)
		if maps.ndim != ntot: maps = maps.reshape(maps.shape[:-2]+(1,)*(maps.ndim-ntot)+maps.shape[-2:])
		return maps

def flat_noise(shape, wcs, sigmas):
	res = en.zeros([len(sigmas),shape[-3],shape[-3],shape[-2],shape[-1]], wcs)
	for i,s in enumerate(sigmas):
		res[i] = (np.eye(shape[-3])*s**2)[:,:,None,None]
	return res

def read_noise(info, shape, wcs, n):
	try:
		nmat = flat_noise(shape, wcs, parse_floats(info))
	except ValueError:
		nmat = read_maps(info, n, 5)
	if len(nmat) != n: raise ValueError("Number of noise maps (%d) != number of signal maps (%d)!" % (len(nmat), n))
	if np.any(nmat.shape[-2:] != shape[-2:]): raise ValueError("Noise and maps have inconsistent shape!")
	return nmat

def parse_floats(strs): return np.array([float(w) for w in strs.split(",")])

def apodize(m, rad, apod_fun):
	scale = m.extent()/m.shape[-2:]
	y = np.arange(m.shape[-2])*scale[0]
	x = np.arange(m.shape[-1])*scale[1]
	yfun = apod_fun(y, rad)*apod_fun(y[-1]-y, rad)
	xfun = apod_fun(x, rad)*apod_fun(x[-1]-x, rad)
	a = yfun[:,None]*xfun[None,:]
	return m*a
def apod_step(x, r): return x>r
def apod_butter(x, r): return (1+(x/r)**-4)**-1
def apod_cos(x,r): return (1-np.cos(np.min(1,nx/r)*np.pi))/2

# Read our inputs
freqs = parse_floats(args.freqs)
maps  = read_maps(args.maps, len(freqs))
ncomp = maps.shape[-3]
nfreq = maps.shape[-4]
noise = read_noise(args.noise, maps.shape, maps.wcs, len(freqs))
ps    = powspec.read_spectrum(args.powspec, expand="diag")[:ncomp,:ncomp]
poss  = np.loadtxt(args.posfile)[:,:2]/r2c
R     = args.radius/r2c/60
beam_fiducial = 1.5/r2b
beam_range = [0.8/r2b,3.0/r2b]
beam_max_asym = 2
apod_rad = R/10

# We will cut out small mini-maps around each source candadate and
# sample the CMB and source parameters jointly. But some candiates
# are so near each other that they aren't independent. These must
# be grouped into groups.
def build_groups(poss):
	def dist(a,b): return np.sum((poss[a]-poss[b])**2)**0.5*180*60/np.pi
	rest   = set(range(len(poss)))
	groups = []
	while len(rest) > 0:
		group = []
		tocheck = [rest.pop()]
		# Find distance to all other points
		while len(tocheck) > 0:
			current = tocheck.pop()
			rnew = set()
			while rest:
				other = rest.pop()
				if dist(current,other) < args.mindist_group:
					tocheck.append(other)
				else:
					rnew.add(other)
			rest = rnew
			group.append(current)
		groups.append(group)
	return groups
groups = build_groups(poss)
print "Found %d groups" % len(groups)

# We will sample (cmb,A,pos,ibeam) jointly in gibbs fashion:
#  cmb,A   <- P(cmb,A|data,A,pos,ibeam)   # direct, but requires cr
#  pos,ibeam <- P(pos,ibeam|data,cmb,A)   # MCMC
# To take into account the nonperiodicity of each submap, we must introduce
# a region of extra noise around the edge.

class CMBSampler:
	"""Draws samples from P(s,a|d,Cl,N,T), where T[ntemp,nfreq,ncomp,ny,nx] is a set of templates.
	a[ntemp] is the set of template amplitudes."""
	def __init__(self, maps, inoise, ps, T=None):
		self.d   = maps
		self.iN  = inoise
		self.hN  = en.multi_pow(inoise, 0.5, axes=[1,2])
		self.iS  = en.spec2flat(maps.shape[-3:], maps.wcs, ps, -1.0)
		self.hS  = en.spec2flat(maps.shape[-3:], maps.wcs, ps, -0.5)
		self.ps  = ps
		self.b, self.x = None, None
		# Prepare the preconditioner. It approximates the noise as the
		# same in every pixel, and ignores the cmb-template coupling.
		# See M(self,u) for details.
		iN_white = np.array(np.sum(np.mean(np.mean(self.iN,-1),-1),0))
		# iN_white is now in pixel space, but the preconditioner needs it
		# in harmonic space, which introduces a 
		#norm = np.prod((maps.box[1]-maps.box[0])/maps.shape[-2:])
		#norm = 1./np.prod(maps.shape[-2:])
		#iN_white /= norm
		self.S_prec = en.multi_pow(self.iS + iN_white[:,:,None,None], -1)

		# The template
		self.set_template(T)
	def set_template(self, T):
		if T is None: T = np.zeros((0,)+self.d.shape)
		self.T   = T
		self.TT = np.einsum("aijyx,bijyx->ab",self.T,self.T)
		self.dof = DOF(Arg(default=self.d[0]), Arg(shape=T.shape[:1]))
	def P(self, u):
		s, a = self.dof.unzip(u)
		return s[None,:,:,:] + np.sum(self.T*a[:,None,None,None,None],0)
	def PT(self, d):
		return self.dof.zip(np.sum(d,0), np.einsum("qijyx,ijyx->q",self.T, d))
	def A(self, u):
		s, a = self.dof.unzip(u)
		# U"u = [S"s, 0a]
		Uu   = self.dof.zip(en.harm2map(en.map_mul(self.iS, en.map2harm(s))),a*0)
		# P'N"P u
		PNPu = self.PT(en.map_mul(self.iN, self.P(u)))
		return Uu + PNPu
	def M(self, u):
		# Multiplying things out, the full expression for A is:
		#  [ S" + sum(N")   sum(N"T) ]
		#  [  sum(T'N")     sum(T'T) ]
		# A reasonable approximation for this is
		#  [ S" + sum(sigma^{-2})    0    ]
		#  [         0           sum(T'T) ]
		# which can be directly inverted.
		s, a = self.dof.unzip(u)
		# Solve for the cmb signal component
		res_s = en.harm2map(en.map_mul(self.S_prec,en.map2harm(s)))
		res_a = np.linalg.solve(self.TT, a)
		return self.dof.zip(res_s, res_a)
	def calc_b(self):
		PNd   = self.PT(en.map_mul(self.iN, self.d))
		Uw1_s = en.harm2map(en.map_mul(self.hS, en.rand_gauss_harm(self.d.shape[-3:],self.d.wcs)))
		Uw1_a = np.zeros(self.T.shape[0])
		Uw1   = self.dof.zip(Uw1_s, Uw1_a)
		PNw2  = self.PT(en.map_mul(self.hN, en.rand_gauss(self.d.shape, self.d.wcs)))
		return PNd + Uw1 + PNw2
	def solve(self, b, x0, verbose=False):
		cg = CG(self.A, b, x0=x0*0, M=self.M)
		while cg.err > 1e-6:
			cg.step()
			if verbose:
				print "%5d %15.7e %15.7e" % (cg.i, cg.err, cg.err_true) #, self.dof.unzip(cg.x)[1]
			#if cg.i % 10 == 0:
			#	s, a = self.dof.unzip(cg.x)
			#	matshow(s[0]); colorbar(); show()
		return cg.x
	def sample(self, verbose=False):
		self.b = self.calc_b()
		if self.x is None: self.x = self.dof.zip(self.d[0], np.zeros(self.T.shape[0]))
		self.x = self.solve(self.b, self.x, verbose)
		return self.dof.unzip(self.x)

class PtsrcModel:
	"""This class converts from point source shape parameters to amplitude
	basis functions."""
	def __init__(self, template):
		self.pos   = template.posmap()
		self.nfreq, self.ncomp = template.shape[:2]
		self.nparam = self.nfreq*self.ncomp
	def get_templates(self, pos, irads):
		x   = utils.rewind(self.pos - pos[:,None,None],0,2*np.pi)
		W   = np.array([[irads[0],irads[2]],[irads[2],irads[1]]])
		xWx = np.sum(np.einsum("ab,byx->ayx", W, x)*x,0)
		profile = np.exp(-0.5*xWx)
		bases = np.eye(self.nfreq*self.ncomp).reshape(self.nfreq*self.ncomp,self.nfreq,self.ncomp)
		return profile[None,None,None]*bases[:,:,:,None,None]
	def get_model(self, amps, pos, irads):
		return np.sum((self.get_templates(pos, irads).T*amps.T).T,0)

class ShapeSampler:
	def __init__(self, maps, inoise, model, amps, pos, pos0, irads, nsamp=200, stepsize=0.02, maxdist=1.5*np.pi/180/60):
		self.maps = maps
		self.inoise = inoise
		self.model= model
		self.nsamp= nsamp
		self.stepsize = stepsize
		self.amps = amps
		self.pos, self.irads = pos, irads
		self.pos0 = pos0
		self.maxdist=maxdist
		self.lik = self.getlik(self.amps, self.pos, self.irads)
	def getlik(self, amps, pos, irads):
		if irads[0] < 0 or irads[1] < 0: return np.inf
		if irads[0]*irads[1]-irads[2]**2 <= 0: return np.inf
		sigma, phi = expand_beam(irads)
		# The beam has a tendency to run off in unrealistic directions,
		# so we need a relatively strong prior on it.
		if np.min(sigma) < beam_range[0] or np.max(sigma) > beam_range[1] or np.max(sigma)/np.min(sigma) > beam_max_asym: return np.inf
		template = self.model.get_model(amps, pos, irads)
		residual = self.maps-template
		tmp = np.einsum("fabyx,abyx->fayx",self.inoise, residual)
		deviation = np.sum((pos-self.pos0)**2)**0.5/self.maxdist
		penalty = 1+max(deviation-1,0)**2
		return 0.5*np.sum(tmp*residual)*penalty
	def newpos(self, pos):
		# Draw pos with gaussian prior centered on previous position
		# With a width given by the fiducial beam size.
		step = self.stepsize
		if np.random.uniform() < 0.1: step*100 # Sometimes try larger steps to break out of ruts
		return pos + np.random.standard_normal(2) * beam_fiducial * self.stepsize
	def newshape(self, irads):
		return irads + np.random.standard_normal(3) * 1.0/beam_fiducial**2 * self.stepsize * 0.5
	def newamp(self, amps):
		return amps + np.random.standard_normal(len(amps)) * 1000 * self.stepsize
	def subsample(self, verbose=False):
		pos = self.newpos(self.pos)
		lik = self.getlik(self.amps, pos, self.irads)
		if np.random.uniform() < np.exp(self.lik-lik):
			self.pos, self.lik = pos, lik
		irads = self.newshape(self.irads)
		lik = self.getlik(self.amps, self.pos, irads)
		if np.random.uniform() < np.exp(self.lik-lik):
			self.irads, self.lik = irads, lik
		amps = self.newamp(self.amps)
		lik = self.getlik(amps, self.pos, self.irads)
		if np.random.uniform() < np.exp(self.lik-lik):
			self.amps, self.lik = amps, lik
		if verbose:
			sigma, phi = expand_beam(self.irads)
			print (" %9.2f"*len(self.amps)+" %10.5f %10.5f %8.3f %8.3f %8.3f") % (tuple(self.amps)+tuple(self.pos*r2c)+tuple(sigma*r2b)+(phi*r2c,))
		return self.amps, self.pos, self.irads
	def sample(self, verbose=False):
		"""Draw a new, uncorrelated sample."""
		for i in range(self.nsamp): self.subsample(verbose)
		return self.amps, self.pos, self.irads

class ShapeSamplerMulti:
	def __init__(self, maps, inoise, model, amps, pos, pos0, irads, nsamp=1500, stepsize=0.02, maxdist=1.5*np.pi/180/60):
		self.samplers = [ShapeSampler(maps, inoise, model, amp1, pos1, pos01, irads1, nsamp=1, stepsize=stepsize, maxdist=maxdist) for amp1, pos1, pos01, irads1 in zip(amps, pos, pos0, irads)]
		self.nsamp   = nsamp
	def sample(self, verbose=False):
		for i in range(self.nsamp):
			for sampler in self.samplers:
				sampler.sample(verbose)
		amps = np.array([s.amps  for s in self.samplers])
		pos  = np.array([s.pos   for s in self.samplers])
		irads= np.array([s.irads for s in self.samplers])
		return amps, pos, irads

class GibbsSampler:
	def __init__(self, maps, inoise, ps, pos0, amp0, irads0, cmb0):
		self.maps   = maps
		self.inoise = inoise
		self.ps     = ps
		self.src_model   = PtsrcModel(maps)
		self.pos, self.amp, self.irads, self.cmb = pos0, amp0, irads0, cmb0
		self.pos0 = pos0
		self.cmb_sampler = CMBSampler(maps, inoise, ps)
	def sample(self, verbose=False):
		# First draw cmb,amp <- P(cmb,amp|data,pos,irads)
		src_template = self.src_model.get_templates(self.pos, self.irads)
		self.cmb_sampler.set_template(src_template)
		self.cmb, self.amp = self.cmb_sampler.sample(verbose)
		# Then draw pos,irads <- P(pos,irads|data,cmb,amp)
		maps_nocmb = self.maps - self.cmb[None,:,:,:]
		shape_sampler = ShapeSampler(maps_nocmb, self.inoise, self.src_model, self.amp, self.pos, self.pos0, self.irads)
		self.amp, self.pos, self.irads = shape_sampler.sample(verbose)
		return self.pos, self.amp, self.irads, self.cmb

class GibbsSamplerMulti:
	"""Like GibbsSampler, but samples multiple points jointly.
	This means that the source amplitude parameters will be arrays."""
	def __init__(self, maps, inoise, ps, pos0, amp0, irads0, cmb0):
		self.maps   = maps
		self.inoise = inoise
		self.ps     = ps
		self.src_model = PtsrcModel(maps)
		self.pos, self.amp, self.irads, self.cmb = pos0, amp0, irads0, cmb0
		self.pos0 = pos0
		self.cmb_sampler = CMBSampler(maps, inoise, ps)
	def sample(self, verbose=False):
		# First draw cmb,amp <- P(cmb,amp|data,pos,irads)
		src_template = np.concatenate([self.src_model.get_templates(pos, irads) for pos,irads in zip(self.pos, self.irads)])
		self.cmb_sampler.set_template(src_template)
		self.cmb, self.amp = self.cmb_sampler.sample(verbose)
		# Separate amps for each source
		self.amp = self.amp.reshape(self.pos.shape[0],-1)
		# Then draw pos,irads <- P(pos,irads|data,cmb,amp)
		maps_nocmb = self.maps - self.cmb[None,:,:,:]
		shape_sampler = ShapeSamplerMulti(maps_nocmb, self.inoise, self.src_model, self.amp, self.pos, self.pos0, self.irads)
		self.amp, self.pos, self.irads = shape_sampler.sample(verbose)
		return self.pos, self.amp, self.irads, self.cmb

def expand_beam(irads):
	C = np.array([[irads[0],irads[2]],[irads[2],irads[1]]])
	E, V = np.linalg.eigh(C)
	phi = np.arctan2(V[1,0],V[0,0])
	sigma = E**-0.5
	if sigma[1] > sigma[0]:
		sigma = sigma[::-1]
		phi += np.pi/2
	phi %= np.pi
	return sigma, phi

def smooth_gauss(m, sigma):
	l = np.sum(m.lmap()**2,0)**0.5
	return np.real(en.ifft(en.fft(m)*np.exp(-0.5*(l*sigma)**2)))

def get_startpoint(maps, inoise, ps, rad=5):
	# Filter away the CMB
	sampler = CMBSampler(maps, inoise, ps, maps[None][:0])
	cmb, _ = sampler.sample()
	residual = maps - cmb[None]
	# Smooth based on fiducial beam
	residual = smooth_gauss(residual, beam_fiducial)
	# Extract best point near center
	cpix = np.array(residual.shape[-2:])/2
	center = np.sum(np.sum((residual[:,:,cpix[0]-rad:cpix[0]+rad,cpix[1]-rad:cpix[1]+rad])**2,0),0)
	I = np.argmax(center)
	ipix = np.unravel_index(I, center.shape)
	pos = center.posmap()[:,ipix[0],ipix[1]]
	return pos

def B(T,nu):
	c = 299792458.0
	h = 6.62606957e-34
	k = 1.3806488e-23
	return 2*h*nu**3/c**2/(np.exp(h*nu/k/T)-1)
def uK2mJ(amp,b1,b2):
	T0 = 2.73; nu=148e9
	dB = B(T0+amp*1e-6,nu)-B(T0,nu)
	return dB*2*np.pi*b1*b2/1e-29

def output_dummy(id):
	with open(args.odir+"/samps%03d.txt" % id, "w") as ofile:
		pass

utils.mkdir(args.odir)

if args.nmax > 0: groups = groups[:args.nmax]

for i in range(myid, len(groups), nproc):
	if i < args.i: continue
	group = groups[i]
	if args.cont:
		# If all our members are done, skip to next group
		try:
			lens = [len(np.loadtxt(args.odir + "/samps%03d.txt" % j)) for j in group]
			if np.min(lens) >= args.nsamp:
				continue
		except IOError: pass
	print "%5d/%d %3d:" % (i+1, len(groups), myid),
	print (" %3d"*len(group)) % tuple(group)
	pos0  = np.array([poss[j] for j in group])
	# Cut out a relevant region
	box      = np.array([np.min(pos0,0)-R,np.max(pos0,0)+R])
	submap   = maps.submap(box)
	if submap.size == 0:
		for g in group:
			output_dummy(g)
		continue
	subnoise = apodize(noise.submap(box), apod_rad, apod_step)
	# Set up initial values for the sampler
	irads    = np.tile(np.array([1/beam_fiducial**2,1/beam_fiducial**2,0]),(len(group),1))
	amp      = np.zeros([len(group),ncomp*nfreq])
	cmb      = submap[0]
	sampler  = GibbsSamplerMulti(submap, subnoise, ps, pos0, amp, irads, cmb)
	# Open ofiles
	ofiles = [open(args.odir + "/samps%03d.txt" % j, "w") for j in group]
	for j in xrange(-args.burnin, args.nsamp):
		pos, amp, irad, cmb = sampler.sample(args.verbose)
		if j >= 0:
			for mypos, myamp, myirad, ofile, isrc in zip(pos, amp, irad, ofiles,group):
				sigma, phi = expand_beam(myirad)
				mJ = uK2mJ(myamp,sigma[0],sigma[1])
				print >> ofile, (" %10.5f"*2 + " %6.1f"*len(myamp) + "%8.3f %8.3f %8.3f" + " %6.2f"*len(mJ)) % (tuple(mypos*r2c)+tuple(myamp)+tuple(sigma*r2b)+(phi*r2c,)+tuple(mJ))
				ofile.flush()
				if args.dump > 0 and j % args.dump == 0:
					dumpdir = args.odir + "/dump%03d" % isrc
					utils.mkdir(dumpdir)
					src = sampler.src_model.get_model(myamp, mypos, myirad)
					residual = submap - src - cmb[None]
					# Cut out our area
					mybox = np.array([poss[isrc]-R,poss[isrc]+R])
					mycmb, myres, mymod, mysub = [a.submap(mybox) for a in [cmb,residual,src,submap]]
					en.write_map(dumpdir + "/cmb%03d.hdf" % j, mycmb)
					en.write_map(dumpdir + "/residual%03d.hdf" % j, myres)
					en.write_map(dumpdir + "/model%03d.hdf" % j, mymod)
					en.write_map(dumpdir + "/submap.hdf", mysub)
